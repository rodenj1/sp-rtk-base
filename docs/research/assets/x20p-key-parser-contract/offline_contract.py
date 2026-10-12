"""Research only: synthetic serializer/parser tests; never opens hardware.
Run from repo root with either scratch environment's Python; --manual-dir is
an optional directory containing 2.00.md, 2.02.md, 2.10.md, 2.11.md Firecrawl
full interface extractions. Output is JSON, written only to --output.
"""
from __future__ import annotations
import argparse, ast, enum, hashlib, json, re, struct, sys
from pathlib import Path
from typing import Literal
import importlib.metadata as md
from pyubx2 import UBXMessage, UBXReader, GET, SET, POLL
from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE as DB
from pyubx2.ubxtypes_get import UBX_PAYLOADS_GET
from pyrtcm import RTCMReader, RTCMMessage
from pyrtcm.rtcmhelpers import calc_crc24q

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / 'src/sp_rtk_base/services/drivers/ublox.py'

def inventory():
    # Execute only exact enum definitions and pure baseline builders/constants,
    # not application imports, constructors, services or hardware paths.
    models = ast.parse((ROOT/'src/sp_rtk_base/models/device_models.py').read_text())
    env = {'enum': enum, 'Literal': Literal}
    names = {'RtcmRowId','RtcmOutputPort','PortId','UbxProtocol','GnssConstellation'}
    for n in models.body:
        if isinstance(n,ast.ClassDef) and n.name in names:
            exec(compile(ast.Module(body=[n],type_ignores=[]),'baseline-enum','exec'),env)
    tree = ast.parse(SRC.read_text())
    wanted = {'_RTCM_PORTS','_RTCM_KEY_BASES','_PROTOCOL_PORT_PREFIXES','_PROTOCOL_DIRECTIONS','_GNSS_SIGNAL_ENA_KEYS','_RECEIVER_SCALAR_KEYS','_MATRIX_PORTS','_TMODE_KEYS','_rtcm_key','_protocol_key'}
    for n in tree.body:
        name = n.name if isinstance(n,ast.FunctionDef) else n.target.id if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) else None
        if name in wanted: exec(compile(ast.Module(body=[n],type_ignores=[]),'baseline-builder','exec'),env)
    groups = {
      'rtcm_read_5_ports': [env['_rtcm_key'](row,p) for row in env['_RTCM_KEY_BASES'] for p in env['_RTCM_PORTS']],
      'rtcm_write_3_ports': [env['_rtcm_key'](row,p.value) for row in env['_RTCM_KEY_BASES'] for p in env['_MATRIX_PORTS']],
      'protocol_3_ports': [env['_protocol_key'](p,d,proto) for p in env['PortId'] for d in env['_PROTOCOL_DIRECTIONS'] for proto in env['UbxProtocol']],
      'correction_reports_3_ports': [f'CFG_MSGOUT_UBX_RXM_RTCM_{p.value}' for p in env['PortId']],
      'scalar': env['_RECEIVER_SCALAR_KEYS'], 'tmode':list(env['_TMODE_KEYS']),
      'rate_writer':['CFG_RATE_MEAS','CFG_RATE_NAV']}
    literals={n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value in DB}
    groups['literal_database_keys']=sorted(literals)
    keys=sorted(set().union(*map(set,groups.values())))
    rows=[]
    for k in keys:
        kid, typ=DB[k]
        rows.append({'key':k,'id':f'0x{kid:08x}','library_type':typ,'groups':[g for g,v in groups.items() if k in v],
        'baseline_literal_lines': sorted({n.lineno for n in ast.walk(tree) if isinstance(n,ast.Constant) and n.value==k})})
    return groups,rows

def frame(cl,mid,payload):
    head=bytes([cl,mid])+len(payload).to_bytes(2,'little')+payload
    a=b=0
    for x in head:a=(a+x)&255;b=(b+a)&255
    return b'\xb5\x62'+head+bytes([a,b])

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--manual-dir');a=ap.parse_args()
    groups,rows=inventory()
    out={'synthetic_not_hardware':True,'python':sys.version,'versions':{k:md.version(k) for k in ['pyubx2','pyrtcm','pynmeagps']},'group_counts':{g:len(v) for g,v in groups.items()},'unique_app_keys':len(rows),'groups':groups,'keys':rows,'tests':[]}
    out['baseline_driver_sha256']=hashlib.sha256(SRC.read_bytes()).hexdigest()
    out['baseline_commit']='8854c750f06ff321be29c9d29f9874484ce92b97'
    out['package_source_sha256']={}
    import importlib
    for module in ['pyubx2.ubxmessage','pyubx2.ubxtypes_get','pyubx2.ubxtypes_configdb','pyrtcm.rtcmmessage','pyrtcm.rtcmtables']:
        file=Path(importlib.import_module(module).__file__)
        out['package_source_sha256'][module]=hashlib.sha256(file.read_bytes()).hexdigest()
    def test(name,fn):
        try: out['tests'].append({'name':name,'result':fn(),'error':None})
        except Exception as e:out['tests'].append({'name':name,'error':type(e).__name__+': '+str(e)})
    for r in rows:
        k=r['key'];typ=r['library_type'];size=int(typ[1:])
        value=-1 if typ.startswith('I') else 1
        def roundtrip(k=k,value=value,size=size):
            sent=UBXMessage.config_set(5,0,[(k,value)]).serialize()
            parsed=UBXReader.parse(sent,msgmode=SET)
            expected=struct.pack('<I',DB[k][0])+value.to_bytes(size,'little',signed=value<0)
            assert sent[10:-2]==expected,(k,sent.hex(),expected.hex())
            # Independently generate a VALGET response with the same raw value.
            recv=frame(6,0x8b,b'\x01\x00\x00\x00'+expected)
            got=UBXReader.parse(recv)
            assert getattr(got,k)==value
            return {'valset_hex':sent.hex(),'valget_hex':recv.hex(),'decoded':getattr(got,k)}
        test('key_roundtrip:'+k,roundtrip)
    for count in (64,65):
        for op in ('poll','set','del'):
            def cap(count=count,op=op):
                keys=['CFG_RATE_MEAS']*count
                msg=UBXMessage.config_poll(0,0,keys) if op=='poll' else UBXMessage.config_set(1,0,[(k,1000) for k in keys]) if op=='set' else UBXMessage.config_del(2,0,keys)
                return len(msg.serialize())
            test(f'cap_{op}_{count}',cap)
    for layer in (0,1,2,7):test('poll_layer_'+str(layer),lambda layer=layer:UBXMessage.config_poll(layer,64,['CFG_RATE_MEAS']).serialize().hex())
    for layer in (1,2,4,5,7):test('set_layer_'+str(layer),lambda layer=layer:UBXMessage.config_set(layer,0,[('CFG_RATE_MEAS',1000)]).serialize().hex())
    def cor():
        status=1|(1<<5)|(2<<7)|(42<<9)|(1<<25)|(1<<27)
        raw=frame(2,0x34,struct.pack('<BBHIHH',1,80,0,status,1077,0))
        p=UBXReader.parse(raw)
        assert (p.protocol,p.errStatus,p.msgUsed,p.correctionId,p.ebno)==(1,1,2,42,10)
        return {'hex':raw.hex(),'attrs':{k:v for k,v in vars(p).items() if not k.startswith('_')}}
    test('RXM_COR',cor)
    test('RXM_RTCM_library_only',lambda:str(UBXReader.parse(frame(2,0x32,struct.pack('<BBHHH',2,4,0,42,1077)))))
    for k in ['CFG_NAVCOR_ENABLE_HOST','CFG_NAVCOR_ENABLE_GAL_HAS','CFG_RTCM_MSM_OUT_RANGERATE_SIGN','CFG_SIGNAL_PLAN','CFG_MSGOUT_RTCM_3X_TYPE1006_USB','CFG_MSGOUT_UBX_RXM_COR_USB','CFG_RTCM_DF028_OUT','CFG_SIGNAL_QZSS_L1CB_ENA','CFG_SIGNAL_NAVIC_ENA','CFG_SIGNAL_NAVIC_L5_ENA']:
        test('candidate_key:'+k,lambda k=k:UBXMessage.config_set(1,0,[(k,1)]).serialize().hex())
    # Numeric unknown/known keys demonstrate version-dependent value typing.
    for value in (1, b'\x01'):
        test('unknown_numeric_sign_key:'+repr(value),lambda value=value:UBXMessage.config_set(1,0,[(0x10090011,value)]).serialize().hex())
    # Independently packed navigation/status payloads; all values synthetic.
    pvt=bytearray(92);struct.pack_into('<I',pvt,0,123000);pvt[20]=3;pvt[21]=1|2|(2<<6);struct.pack_into('<iiiiII',pvt,24,123456789,-234567890,1234,1000,25,30);struct.pack_into('<H',pvt,78,3<<1)
    def pvt_check():
        p=UBXReader.parse(frame(1,7,pvt));assert (p.carrSoln,p.diffSoln,p.lastCorrectionAge,p.lon,p.lat,p.height)==(2,1,3,12.3456789,-23.456789,1234)
        return {k:getattr(p,k) for k in ['carrSoln','diffSoln','lastCorrectionAge','lon','lat','height']}
    test('NAV_PVT_fields',pvt_check)
    hp=struct.pack('<BBBBIiiibbbBI',0,0,0,0,123000,12345,-12345,12345,10,-10,0,0,1234)
    def hp_check():
        p=UBXReader.parse(frame(1,0x13,hp));assert (p.ecefX,p.ecefY,p.pAcc,p.invalidEcef)==(12345.1,-12345.1,123.4,0)
        return {'ecefX_cm':p.ecefX,'ecefY_cm':p.ecefY,'pAcc_mm':p.pAcc}
    test('NAV_HPPOSECEF_units',hp_check)
    def sat_check():
        raw=frame(1,0x35,struct.pack('<IBBHBBBBhhI',123000,1,1,0,0,1,45,30,90,0,8|(1<<17)))
        p=UBXReader.parse(raw);assert (p.svUsed_01,p.rtcmCorrUsed_01,p.elev_01)==(1,1,30)
        return str(p)
    test('NAV_SAT_used_flags',sat_check)
    def sig_check():
        raw=frame(1,0x43,struct.pack('<IBBHBBBBhBBBBHI',123000,0,1,0,0,1,6,0,0,45,7,1,0,(1<<3)|(1<<6),0))
        p=UBXReader.parse(raw);assert (p.sigId_01,p.corrSource_01,p.prUsed_01,p.prCorrUsed_01)==(6,1,1,1)
        return str(p)
    test('NAV_SIG_source_and_used_flags',sig_check)
    def comms_check():
        raw=frame(10,0x36,struct.pack('<BBBB4B',0,1,0,0,0,1,5,255)+struct.pack('<HHIBBHIBBH4H8sI',3,0,100,1,2,0,200,3,4,0,10,20,30,0,b'\x00'*8,5))
        p=UBXReader.parse(raw);assert (p.portId_01,p.rxBytes_01,p.protIds_03,p.msgs_01_03,p.skipped_01)==(3,200,5,30,5)
        return str(p)
    test('MON_COMMS_fields',comms_check)
    def svin_check():
        payload=struct.pack('<B3sIIiiibbbBII BB2s',0,b'\x00'*3,123000,60,12345,-12345,12345,10,-10,0,0,1234,100,1,0,b'\x00'*2)
        p=UBXReader.parse(frame(1,0x3b,payload));assert (p.meanX,p.meanXHP,p.meanAcc,p.valid,p.active)==(12345,10,1234,1,0)
        return str(p)
    test('NAV_SVIN_separate_HP_and_accuracy',svin_check)
    for name in ['NAV-PVT','NAV-SAT','NAV-SIG','NAV-SVIN','NAV-HPPOSECEF','MON-COMMS','RXM-COR']:
        out.setdefault('schemas',{})[name]=UBX_PAYLOADS_GET[name]
    # Reuse baseline pure MSM encoder nodes; no app package or serial imports.
    ft=ast.parse((ROOT/'tests/unit/msm_frames.py').read_text())
    env={'Frame':type('Frame',(),{'__init__':lambda self,**kw:self.__dict__.update(kw)}), 'RTCMMessageDecoder':type('Decoder',(),{'calc_crc24q':staticmethod(calc_crc24q)})}
    exec(compile(ast.Module(body=[n for n in ft.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in ['_Bits','_frame','msm_frame']],type_ignores=[]),'baseline-msm','exec'),env)
    from pyrtcm import rtcmtables
    out['rtcm_signal_tables']={k:v for k,v in vars(rtcmtables).items() if k.endswith('SIG_MAP')}
    for msg,sigs in [(1077,[22,23,24]),(1097,[8,9,10,22,23,24]),(1127,[8,9,10,22,23,24]),(1117,[22]),(1137,[22])]:
        for sig in sigs:
            def msm(msg=msg,sig=sig):
                raw=env['msm_frame'](msg,{1:{sig:45.0}},epoch_ms=123000).data
                p=RTCMReader.parse(raw); assert p.identity==str(msg); assert p.serialize()==raw
                return {'hex':raw.hex(),'signal_id':sig,'decoded_signal':getattr(p,'CELLSIG_01',None),'cn0':getattr(p,'DF408_01',None)}
            test(f'MSM_{msg}_signal_{sig}',msm)
    out['research_catalog']={k:{'id':f'0x{v[0]:08x}','library_type':v[1]} for k,v in DB.items() if k.startswith(('CFG_SIGNAL_','CFG_NAVCOR_','CFG_RTCM_')) or 'TYPE1006_' in k or 'UBX_RXM_COR_' in k or 'UBX_NAV2_' in k}
    try:
        from sp_rtk_base_relay.rtcm_decoder import RTCMMessageDecoder
        out['relay_version']=md.version('sp-rtk-base-relay')
        def relay_check():
            results=[]
            for msg in [1077,1097,1127,1117,1137,4090]:
                raw=env['msm_frame'](msg if msg!=4090 else 1077,{1:{22:45.0}}).data
                if msg==4090:
                    payload=(msg<<4).to_bytes(2,'big')+b'\x00';body=b'\xd3\x00\x03'+payload;raw=body+calc_crc24q(body).to_bytes(3,'big')
                assert RTCMMessageDecoder.is_valid_rtcm_frame(raw)
                assert RTCMMessageDecoder.extract_message_id(raw)==msg
                results.append({'id':msg,'hex':raw.hex(),'crc_valid':True})
            return results
        test('relay_opaque_new_signal_and_unknown_id',relay_check)
    except ImportError:
        out['relay_note']='not installed in candidate environment; relay unchanged'
    if a.manual_dir:
        for version in ['2.00','2.02','2.10','2.11']:
            text=(Path(a.manual_dir)/(version+'.md')).read_text()
            for r in rows:
                official=r['key'].replace('_','-',2) # CFG_GROUP_ITEM, item underscores preserved
                matches=list(re.finditer(re.escape(official)+r'\s+(0x[0-9a-fA-F]{8})\s*([LEUIXR][1248]?)',text))
                evidence=[]
                for m in matches:
                    section=text[:m.start()];following=text[m.end():m.end()+300]
                    page=re.search(r'Page (\d+) of \d+',text[m.end():])
                    evidence.append({'id':m.group(1),'type':m.group(2),'extraction_line':section.count('\n')+1,'printed_page':int(page.group(1)) if page else None,'following_text':following})
                r.setdefault('firmware',{})[version]={'status':'documented_catalog' if matches else 'absent_from_catalog_not_hardware_rejection','evidence':evidence}
    Path(a.output).write_text(json.dumps(out,indent=2,default=str)+'\n')
    print(json.dumps({'versions':out['versions'],'unique_keys':len(rows),'groups':out['group_counts'],'test_count':len(out['tests']),'errors':[{'name':x['name'],'error':x['error']} for x in out['tests'] if x['error']]},indent=2))
if __name__=='__main__':main()
