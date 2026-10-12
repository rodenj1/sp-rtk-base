"""Aggregate persisted real offline results; generate CSV and diff evidence.
Run from repo root: python docs/research/assets/x20p-key-parser-contract/aggregate.py
Requires both offline_contract.py result files and --manual-dir for supplements.
"""
import argparse,csv,hashlib,json,re
from pathlib import Path
P=Path(__file__).resolve().parent
ap=argparse.ArgumentParser();ap.add_argument('--manual-dir',required=True);args=ap.parse_args()
a=json.loads((P/'locked-results.json').read_text());b=json.loads((P/'candidate-results.json').read_text())
assert len(a['keys'])==len({r['key'] for r in a['keys']})==112
for x,expected in [(a,{'cap_poll_65','cap_set_65','cap_del_65','candidate_key:CFG_NAVCOR_ENABLE_HOST','candidate_key:CFG_NAVCOR_ENABLE_GAL_HAS','candidate_key:CFG_RTCM_MSM_OUT_RANGERATE_SIGN','candidate_key:CFG_RTCM_DF028_OUT','candidate_key:CFG_SIGNAL_QZSS_L1CB_ENA','unknown_numeric_sign_key:1'}),(b,{'cap_poll_65','cap_set_65','cap_del_65',"unknown_numeric_sign_key:b'\\x01'"})]:
    errors={t['name'] for t in x['tests'] if t['error']}
    assert errors==expected,(errors,expected)
    assert sum(t['name'].startswith('key_roundtrip:') and t['error'] is None for t in x['tests'])==112

def unit(k):
    if k.startswith('CFG_MSGOUT'):return 'output divider; 0 off; N every N applicable events/epochs'
    if 'BAUDRATE' in k:return 'baud (bit/s)'
    if k=='CFG_RATE_MEAS':return 'ms (scale 0.001 s)'
    if k=='CFG_RATE_NAV':return 'measurement/solution ratio'
    if k=='CFG_NAVSPG_INFIL_MINELEV':return 'degree'
    if 'TMODE' in k:
        if k.endswith(('POS_TYPE','MODE')):return 'enum'
        if k.endswith(('LAT_HP','LON_HP')):return '1e-9 degree; signed -99..99 residual'
        if k.endswith(('LAT','LON')):return '1e-7 degree'
        if k.endswith(('ACC','ACC_LIMIT')) or k.endswith('_HP'):return '0.1 mm; HP signed -99..99 residual where applicable'
        if k.endswith('MIN_DUR'):return 's'
        return 'cm'
    return 'boolean' if 'ENA' in k or 'PROT_' in k else 'enum'

with (P/'app-key-contract.csv').open('w') as f:
    fields=['key','id','type','unit','layer_contract','app_groups','2.00','2.02','2.10','2.11','caveat','primary_pages']
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
    for r in a['keys']:
        row={'key':r['key'],'id':r['id'],'type':r['library_type'][0]+str(int(r['library_type'][1:])),'unit':unit(r['key']),'layer_contract':'RAM effective; BBR/Flash saved overrides; Default read-only; storage availability must be verified','app_groups':';'.join(r['groups']),'caveat':'','primary_pages':';'.join(v+':'+','.join(str(e['printed_page']) for e in x['evidence']) for v,x in r['firmware'].items())}
        for v,x in r['firmware'].items():
            row[v]='documented' if x['evidence'] else 'absent/unknown; not observed NAK'
            for e in x['evidence']:
                assert e['id'].lower()==r['id']
                assert e['type']==row['type'] or (row['type']=='L1' and e['type']=='L')
        if 'GLO_' in r['key'] or any(s in r['key'] for s in ['TYPE1084','TYPE1087','TYPE1230']):row['caveat']='2.00/2.02 generic catalog only; usable X20P GLONASS unknown. 2.10/2.11 region-gated.'
        if 'TYPE4072' in r['key']:row['caveat']='2.10/2.11 output documented; 4072.1 operational use unresolved; not required for stationary base'
        if r['key']=='CFG_SIGNAL_BDS_B2_ENA':row['caveat']='B2I, not B2a; generic catalog does not establish usable B2I; supported signal lists omit B2I'
        if '_USB' in r['key'] or r['key'].startswith('CFG_USB'):row['caveat']+='; 2.00 USB undocumented; carrier reachability not established by key'
        if 'RXM_RTCM' in r['key']:row['caveat']='library recognizes F9 key; absent all X20 catalogs; use firmware-qualified RXM-COR output key instead'
        w.writerow(row)

manuals={v:(Path(args.manual_dir)/(v+'.md')).read_text() for v in ['2.00','2.02','2.10','2.11']}
extras={k:v for k,v in b['research_catalog'].items() if k.startswith(('CFG_NAVCOR_','CFG_RTCM_')) or 'TYPE1006_' in k or 'UBX_RXM_COR_' in k or k in ['CFG_SIGNAL_PLAN','CFG_SIGNAL_QZSS_L1CB_ENA','CFG_SIGNAL_GPS_L5_ENA','CFG_SIGNAL_GAL_E5A_ENA','CFG_SIGNAL_GAL_E6_ENA','CFG_SIGNAL_BDS_B1C_ENA','CFG_SIGNAL_BDS_B2A_ENA','CFG_SIGNAL_BDS_B3_ENA','CFG_SIGNAL_NAVIC_ENA','CFG_SIGNAL_NAVIC_L5_ENA']}
supp=[]
for k,db in sorted(extras.items()):
    r={'key':k,**db,'locked_name_available':k in a['research_catalog'],'firmware':{}}
    for v,text in manuals.items():
        m=re.search(re.escape(k.replace('_','-',2))+r'\s+(0x[0-9a-fA-F]{8})\s*([LEUIXR][1248]?)',text)
        r['firmware'][v]={'status':'documented' if m else 'absent/unknown','id':m.group(1) if m else None,'type':m.group(2) if m else None,'text':text[m.end():m.end()+240] if m else None}
        if k=='CFG_NAVCOR_ENABLE_HOST' and v=='2.11':r['firmware'][v]['status']='removed explicitly in R11 section 2.1.2'
        if m:assert m.group(1).lower()==db['id']
    supp.append(r)
(P/'supplemental-contract.json').write_text(json.dumps(supp,indent=2)+'\n')
# Exact baseline message identities, including dynamic NAV poll argument strings.
import ast
source=(P.parents[3]/'src/sp_rtk_base/services/drivers/ublox.py').read_text()
tree=ast.parse(source)
message_names=sorted({n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and re.fullmatch(r'(NAV|MON|CFG|ACK|RXM)-[A-Z0-9]+',n.value)})
messages=[]
for name in message_names+['RXM-COR']:
    r={'identity':name,'baseline_literal_lines':sorted({n.lineno for n in ast.walk(tree) if isinstance(n,ast.Constant) and n.value==name}),'firmware':{}}
    for v,text in manuals.items():
        m=re.search(r'UBX-'+name+r'\s*(0x[0-9a-fA-F]{2})\s*(0x[0-9a-fA-F]{2})',text)
        r['firmware'][v]={'status':'documented' if m else 'absent_from_detailed_inventory','class_id':m.group(1)+' '+m.group(2) if m else None}
        if v=='2.00' and name in ['CFG-VALGET','CFG-VALSET','CFG-VALDEL']:r['firmware'][v]['status']='supported high-level section 6.4.1; detailed wire description absent'
    messages.append(r)
(P/'message-contract.json').write_text(json.dumps(messages,indent=2)+'\n')
summary={'baseline_message_identities':len(message_names),'unique_app_keys':112,'group_counts':a['group_counts'],'catalog_present_counts':{v:sum(bool(r['firmware'][v]['evidence']) for r in a['keys']) for v in manuals},'locked_tests':len(a['tests']),'candidate_tests':len(b['tests']),'locked_expected_errors':[t for t in a['tests'] if t['error']],'candidate_expected_errors':[t for t in b['tests'] if t['error']],'supplemental_keys':len(supp),'signal_tables_equal':a['rtcm_signal_tables']==b['rtcm_signal_tables'],'candidate_research_key_additions':{k:v for k,v in b['research_catalog'].items() if k not in a['research_catalog']},'manual_sha256':{v:hashlib.sha256((Path(args.manual_dir)/(v+'.md')).read_bytes()).hexdigest() for v in manuals}}
(P/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2))
