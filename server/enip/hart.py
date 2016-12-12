#! /usr/bin/env python3

# 
# Cpppo -- Communication Protocol Python Parser and Originator
# 
# Copyright (c) 2013, Hard Consulting Corporation.
# 
# Cpppo is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  See the LICENSE file at the top of the source tree.
# 
# Cpppo is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# 

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

__author__                      = "Perry Kundert"
__email__                       = "perry@hardconsulting.com"
__copyright__                   = "Copyright (c) 2013 Hard Consulting Corporation"
__license__                     = "Dual License: GPLv3 (or later) and Commercial (see LICENSE)"


"""
enip.hart	-- Implements I/O to HART devices via a C*Logix HART Interface

    http://literature.rockwellautomation.com/idc/groups/literature/documents/um/1756-um533_-en-p.pdf

    There are 16 HART channels numbered 0..15.  An Instance of the HART Object represents a Channel.
    
     | Channel | Instance |
     |---------+----------|
     | 0       |        1 |
     | ...     |      ... |
     | 15      |       16 |


    A cpppo.server.enip.hart 'hart_io' object is a proxy to a C*Logix HART I/O module.

"""

import json
import logging
import sys
import threading
import traceback

from ...dotdict import dotdict
from ... import automata, misc
from .device import ( Attribute, Object,
                      Message_Router, Connection_Manager, UCMM, Identity, TCPIP,
                      resolve_element, resolve_tag, resolve, redirect_tag, lookup )
from .parser import ( BOOL, UDINT, DINT, UINT, INT, USINT, SINT, REAL, EPATH, typed_data,
                      move_if, octets_drop, octets_noop, enip_format, status )
from .logix import Logix

log				= logging.getLogger( "enip.hart" )

from .get_attribute import proxy

# Create all the specified tags/Attributes.  The enip_process function will (somehow) assign the
# given tag name to reference the specified Attribute.  We'll define an Attribute to print
# I/O if args.print is specified; reads will only be logged at logging.NORMAL and above.
class Attribute_print( Attribute ):
    def __getitem__( self, key ):
        value		= super( Attribute_print, self ).__getitem__( key )
        if log.isEnabledFor( logging.NORMAL ):
            print( "%20s[%5s-%-5s] == %s" % (
                self.name, 
                key.indices( len( self ))[0]   if isinstance( key, slice ) else key,
                key.indices( len( self ))[1]-1 if isinstance( key, slice ) else key,
                value ))
        return value

    def __setitem__( self, key, value ):
        super( Attribute_print, self ).__setitem__( key, value )
        print( "%20s[%5s-%-5s] <= %s" % (
            self.name, 
            key.indices( len( self ))[0]   if isinstance( key, slice ) else key,
            key.indices( len( self ))[1]-1 if isinstance( key, slice ) else key,
            value ))

class HART_Data( Logix ): # Must understand Read Tag [Fragmented], etc.
    class_id			= 0xF35D

class HART( Message_Router ):

    """A HART Interface Object.  Represents a Channel.  Understands Get/Set Attribute ..., and the
    HART-specific Service Codes:

    | Class | Service Code | Function                    |
    |-------+--------------+-----------------------------|
    | 0x35D |         0x4B | Read Dynamic Variables      |
    |       |         0x4C | Read Additional Status      |
    |       |         0x4D | Get HART Device Information |

    | Class | Service Code | Pass-through Messages |
    |-------+--------------+-----------------------|
    | 0x35D |         0x4E | Init                  |
    |       |         0x4F | Query                 |
    |       |         0x50 | Flush Queue           |


    Shouldn't really understand Message_Router concepts (eg. Multiple Service Packet requests)
    However, since we may want to use it as a cpppo.server.enip.device 'dialect' (ie. for
    parsing/producing I/O to a C*Logix), we'll derive it from Message_Router (instead of just
    Object).  Since the service codes overlap w/ *Logix Read/Write Tag [Fragmented], we can't use
    both HART and C*Logix Tag requests -- the Multiple Service Packet will not be able to parse
    them, as there is no "device.dialect" that understands both.  So, we can't use Multiple Service
    Packets containing both in a simulator using HART as the device.dialect...

    The HART I/O card also operates as its own Connection Manager (vs. Class 0x06, Instance 1, as in
    a C*Logix PLC).  This means that we must also recognize the 0x52 Unconnected Send encapsulation,
    parse our own requests and send them to ourself.

    Will search for a Tag eg. "HART_0_Data.PV" (REAL), when accessing data to fulfil a request.

    """
    class_id			= 0x35D

    RD_VAR_NAM			= "Read Dynamic Variable"
    RD_VAR_CTX			= "read_var"
    RD_VAR_REQ			= 0x4B
    RD_VAR_RPY			= RD_VAR_REQ | 0x80
    RD_VAR_RPY_FLD		= (
	# The type of the 'default' is retained for all data written!
        # Type Class	Tag			Default
        ( USINT,	'HART_command_status',	0 ),
        ( USINT,	'HART_fld_dev_status',	0 ),
        ( USINT,	'HART_ext_dev_status',	0 ),
        ( REAL,		'PV',			0.0 ),
        ( REAL,		'SV',			0.0 ),
        ( REAL,		'TV',			0.0 ),
        ( REAL,		'FV',			0.0 ),
        ( USINT,	'PV_units',		0 ),
        ( USINT,	'SV_units',		0 ),
        ( USINT,	'TV_units',		0 ),
        ( USINT,	'FV_units',		0 ),
        ( USINT,	'PV_assignment_code',	0 ),
        ( USINT,	'SV_assignment_code',	0 ),
        ( USINT,	'TV_assignment_code',	0 ),
        ( USINT,	'FV_assignment_code',	0 ),
        ( USINT,	'PV_status',		0 ),
        ( USINT,	'SV_status',		0 ),
        ( USINT,	'TV_status',		0 ),
        ( USINT,	'FV_status',		0 ),
        ( REAL,		'loop_current',		0.0 ),
    )
    
    RD_STS_NAM			= "Read Additional Status"
    RD_STS_CTX			= "read_sts"
    RD_STS_REQ			= 0x4C
    RD_STS_RPY			= RD_STS_REQ | 0x80

    RD_INF_NAM			= "Get Device Info"
    RD_INF_CTX			= "get_info"
    RD_INF_REQ			= 0x4D
    RD_INF_RPY			= RD_INF_REQ | 0x80

    PT_INI_NAM			= "Init"
    PT_INI_CTX			= "init"
    PT_INI_REQ			= 0x4E
    PT_INI_RPY			= PT_INI_REQ | 0x80

    PT_QRY_NAM			= "Query"
    PT_QRY_CTX			= "query"
    PT_QRY_REQ			= 0x4F
    PT_QRY_RPY			= PT_QRY_REQ | 0x80

    PT_FLQ_NAM			= "Flush Queue"
    PT_FLQ_CTX			= "flush"
    PT_FLQ_REQ			= 0x50
    PT_FLQ_RPY			= PT_FLQ_REQ | 0x80

    def request( self, data ):
        """Any exception should result in a reply being generated with a non-zero status."""

        # Unconnected Send (0x52)?  Act as our own Connection Manager.
        if data.get( 'service' ) == Connection_Manager.UC_SND_REQ:
            source		= automata.rememberable( data.request.input )
            try: 
                with self.parser as machine:
                    for i,(m,s) in enumerate( machine.run( path='request', source=source, data=data )):
                        pass
                        #log.detail( "%s #%3d -> %10.10s; next byte %3d: %-10.10r: %s",
                        #            machine.name_centered(), i, s, source.sent, source.peek(),
                        #            repr( data ) if log.getEffectiveLevel() < logging.DETAIL else misc.reprlib.repr( data ))
        
                #log.info( "%s Executing: %s", self, enip_format( data.request ))
                return self.request( data.request )
            except:
                # Parsing failure.  We're done.  Suck out some remaining input to give us some context.
                processed		= source.sent
                memory			= bytes(bytearray(source.memory))
                pos			= len( source.memory )
                future			= bytes(bytearray( b for b in source ))
                where			= "at %d total bytes:\n%s\n%s (byte %d)" % (
                    processed, repr(memory+future), '-' * (len(repr(memory))-1) + '^', pos )
                log.error( "EtherNet/IP CIP error %s\n", where )
                raise
        
        
        # See if this request is for us; if not, route to the correct Object, and return its result.
        # If the resolution/lookup fails (eg. bad symbolic Tag); ignore it (return False on error)
        # and continue processing, so we can return a proper .status error code from the actual
        # request, below.
        target			= self.route( data, fail=Message_Router.ROUTE_FALSE )
        if target:
            if log.isEnabledFor( logging.DETAIL ):
                log.detail( "%s Routing to %s: %s", self, target, enip_format( data ))
            return target.request( data )

        if log.isEnabledFor( logging.DETAIL ):
            log.detail( "%s Request: %s", self, enip_format( data ))
        # This request is for this Object.
        
        # Pick out our services added at this level.  If not recognized, let superclass try; it'll
        # return an appropriate error code if not recognized.
        if ( data.get( 'service' ) == self.RD_VAR_REQ
             or self.RD_VAR_CTX in data and data.setdefault( 'service', self.RD_VAR_REQ ) == self.RD_VAR_REQ ):
            # Read Dynamic Variable --> Read Dynamic Variable Reply.
            pass
        elif ( data.get( 'service' ) == self.RD_STS_REQ
             or self.RD_STS_CTX in data and data.setdefault( 'service', self.RD_STS_REQ ) == self.RD_STS_REQ ):
            # Read Additional Status --> Read Additional Status Reply.
            pass
        elif ( data.get( 'service' ) == self.RD_INF_REQ
             or self.RD_INF_CTX in data and data.setdefault( 'service', self.RD_INF_REQ ) == self.RD_INF_REQ ):
            # Get Device Info --> Get Device Info Reply.
            pass
        elif ( data.get( 'service' ) == self.PT_INI_REQ
             or self.PT_INI_CTX in data and data.setdefault( 'service', self.PT_INI_REQ ) == self.PT_INI_REQ ):
            # Pass-thru Init --> Pass-thru Init Reply.
            pass
        elif ( data.get( 'service' ) == self.PT_QRY_REQ
             or self.PT_QRY_CTX in data and data.setdefault( 'service', self.PT_QRY_REQ ) == self.PT_QRY_REQ ):
            # Pass-thru Query --> Pass-thru Query Reply.
            pass
        elif ( data.get( 'service' ) == self.PT_FLQ_REQ
             or self.PT_FLQ_CTX in data and data.setdefault( 'service', self.PT_FLQ_REQ ) == self.PT_FLQ_REQ ):
            # Pass-thru Flush Queue --> Pass-thru Flush Queue Reply.
            pass
        else:
            # Not recognized; more generic command?
            return super( HART, self ).request( data )

        # It is a recognized HART Object request.  Set the data.status to the appropriate error
        # code, should a failure occur at that location during processing.  We will be returning a
        # reply beyond this point; any exceptions generated will be captured, logged and an
        # appropriate reply .status error code returned.

        data.service           |= 0x80
        data.status		= 0x08		# Service not supported, if not recognized or fail to access
        try:
            if data.service == self.RD_VAR_RPY:
                data.read_var = dotdict()
                for typ,fldnam,dfl in self.RD_VAR_RPY_FLD:
                    insnam	= "HART_{channel}_Data".format( channel=self.instance_id - 1 )
                    tag		= '.'.join( (insnam, fldnam) )
                    res		= resolve_tag( tag )
                    if not res:
                        # Not found; create one.  Use Class ID 0xF35D, same Instance ID as self.
                        # No one else should be creating Instances of this Class ID...
                        clsid	= HART_Data.class_id
                        insid	= self.instance_id
                        obj	= lookup( clsid, insid )
                        if not obj:
                            obj	= HART_Data( insnam, instance_id=insid )
                        att	= Attribute_print( name=tag, type_cls=typ, default=dfl ) # eg. 'PV', REAL
                        attid	= 0
                        if obj.attribute:
                            attid= int( sorted( obj.attribute, key=misc.natural )[-1] )
                        attid    += 1
                        obj.attribute[str(attid)] \
                            	= att
                        log.normal( "%-24s Instance %3d, Attribute %3d added: %s (Tag: %s)", obj, insid, attid, att, tag )
                        res	= redirect_tag( tag, { 'class': clsid, 'instance': insid, 'attribute': attid } )
                        assert resolve_tag( tag ) == res, \
                            "Failed to create '{tag}' Tag pointing to {res!r}; found: {out!r}".format(
                                tag=tag, res=res, out=resolve_tag( tag ))
                    # res is a (clsid,insid,attid) of an Attribute containing this fldnam's data.
                    attribute	= lookup( *res )
                    data.read_var[fldnam]= attribute[0]
                    logging.detail( "%s <-- %s == %s", fldnam, attribute, data.read_var[fldnam] )
                data.status		= 0x00
            else:
                assert False, "Not Implemented: {data!r}".format( data=data )

            # Success (data.status == 0x00), or failure w/ non-zero data.status

        except Exception as exc:
            # On Exception, if we haven't specified a more detailed error code, return General
            # Error.  Remember: 0x06 (Insufficent Packet Space) is a NORMAL response to a successful
            # Read Tag Fragmented that returns a subset of the requested data.
            log.normal( "%r Service 0x%02x %s failed with Exception: %s\nRequest: %s\n%s", self,
                         data.service if 'service' in data else 0,
                         ( self.service[data.service]
                           if 'service' in data and data.service in self.service
                           else "(Unknown)"), exc, enip_format( data ),
                         ( '' if log.getEffectiveLevel() >= logging.NORMAL
                           else ''.join( traceback.format_exception( *sys.exc_info() ))))
            assert data.status, \
                "Implementation error: must specify .status before raising Exception!"
            pass

        # Always produce a response payload; if a failure occurred, will contain an error status
        if log.isEnabledFor( logging.DETAIL ):
            log.detail( "%s Response: Service 0x%02x %s %s", self,
                        data.service if 'service' in data else 0,
                        ( self.service[data.service]
                          if 'service' in data and data.service in self.service
                          else "(Unknown)"), enip_format( data ))
        data.input		= bytearray( self.produce( data ))
        return True

    @classmethod
    def produce( cls, data ):
        """Expects to find .service and/or .<logix-command>, and produces the request/reply encoded to
        bytes.  Defaults to produce the request, if no .service specified, and just
        .read/write_tag/frag found.
         
        A .status of 0x06 in the read_tag/frag reply indicates that more data is available; it is
        not a failure.

        """
        result			= b''
        if ( data.get( 'service' ) == cls.RD_VAR_REQ
             or cls.RD_VAR_CTX in data and data.setdefault( 'service', cls.RD_VAR_REQ ) == cls.RD_VAR_REQ ):
            result	       += USINT.produce(	data.service )
        elif data.get( 'service' ) == cls.RD_VAR_RPY:
            result	       += USINT.produce(	data.service )
            result	       += USINT.produce(	data.status )
            if data.status == 0x00:
                for typ,fld,dfl in cls.RD_VAR_RPY_FLD:
                    result     += typ.produce( data.read_var.get( fld, 0 )) # eg. 'read_var.PV'
        else:
            result		= super( HART, cls ).produce( data )
        return result


def __read_var():
    srvc			= USINT(		  	context='service' )
    srvc[None]		= mark	= octets_noop(			context=HART.RD_VAR_CTX,
                                                terminal=True )
    mark.initial[None]		= move_if( 	'mark',		initializer=True )
    return srvc
HART.register_service_parser( number=HART.RD_VAR_REQ, name=HART.RD_VAR_NAM,
                               short=HART.RD_VAR_CTX, machine=__read_var() )

def __read_var_reply():
    srvc			= USINT( 'service',		context='service' )
    srvc[None]		= stts	= USINT( 'status',		context='status' ) # single byte status (no ext_status)
    stts[None]		= schk	= octets_noop(	'check',
                                                terminal=True )

    hsts			= USINT( 'HART_command_status',	context='read_var', extension='.HART_command_status' )
    hsts[True]		= hfds	= USINT( 'HART_fld_dev_status', context='read_var', extension='.HART_fld_dev_status' )
    hfds[True]		= heds	= USINT( 'HART_ext_dev_status',	context='read_var', extension='.HART_ext_dev_status' )
    heds[True]		= hPVd	= REAL( 'PV',			context='read_var', extension='.PV' )
    hPVd[True]		= hSVd	= REAL( 'SV',			context='read_var', extension='.SV' )
    hSVd[True]		= hTVd	= REAL( 'TV',			context='read_var', extension='.TV' )
    hTVd[True]		= hFVd	= REAL( 'FV',			context='read_var', extension='.FV' )
    hFVd[True]		= hPVu	= USINT( 'PV_units',		context='read_var', extension='.PV_units' )
    hPVu[True]		= hSVu	= USINT( 'SV_units',		context='read_var', extension='.SV_units' )
    hSVu[True]		= hTVu	= USINT( 'TV_units',		context='read_var', extension='.TV_units' )
    hTVu[True]		= hFVu	= USINT( 'FV_units',		context='read_var', extension='.FV_units' )
    hFVu[True]		= hPVa	= USINT( 'PV_assignment_code',	context='read_var', extension='.PV_assignment_code' )
    hPVa[True]		= hSVa	= USINT( 'SV_assignment_code',	context='read_var', extension='.SV_assignment_code' )
    hSVa[True]		= hTVa	= USINT( 'TV_assignment_code',	context='read_var', extension='.TV_assignment_code' )
    hTVa[True]		= hFVa	= USINT( 'FV_assignment_code',	context='read_var', extension='.FV_assignment_code' )
    hFVa[True]		= hPVs	= USINT( 'PV_status',		context='read_var', extension='.PV_status' )
    hPVs[True]		= hSVs	= USINT( 'SV_status',		context='read_var', extension='.SV_status' )
    hSVs[True]		= hTVs	= USINT( 'TV_status',		context='read_var', extension='.TV_status' )
    hTVs[True]		= hFVs	= USINT( 'FV_status',		context='read_var', extension='.FV_status' )
    hFVs[True]			= REAL( 'loop_current',		context='read_var', extension='.loop_current',
                                        terminal=True )

    # For status 0x00 (Success), Read Dynamic Variable data follows.  If failed, mark as a read_var
    # request.  Otherwise, continue parsing with HART Command Status
    schk[None]			= automata.decide( 'ok',	state=hsts,
        predicate=lambda path=None, data=None, **kwds: data[path+'.status' if path else 'status'] == 0x00 )
    schk[None]			= move_if(	'mark',		initializer=True,
		                                                destination='read_var' )
    return srvc
HART.register_service_parser( number=HART.RD_VAR_RPY, name=HART.RD_VAR_NAM + " Reply",
                               short=HART.RD_VAR_CTX, machine=__read_var_reply() )


# 
# proxy_hart	-- Example of CIP device proxy: to a C*Logix w/ a HART Interface.
# 
#     All client.connectors must use the same (global) CIP device.dialect; it is safest to specify
# it globally, but we'll ensure it is specified it.
# 
class proxy_hart( proxy ):
    def __init__( self, *args, **kwds ):
        assert kwds.get( 'dialect' ) in ( None, HART )
        kwds['dialect']		= HART
        super( proxy_hart, self ).__init__( *args, **kwds )
