from pathlib import Path
import argparse,yaml
from .core import target_keys
def load(p):
 x=yaml.safe_load(Path(p).read_text(encoding='utf-8'))
 if not x.get('targets'): raise ValueError('targets 不能为空')
 if x.get('limits',{}).get('max_orders',0)<1: raise ValueError('max_orders 必须大于 0')
 return x
def main():
 p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
 for c in ('check','preview'):
  q=s.add_parser(c);q.add_argument('--config',required=True)
 a=p.parse_args();x=load(a.config);print(f"配置有效：{len(x['targets'])} 个目标，订单上限 {x['limits']['max_orders']}")
 for t in x['targets']: print(f"- {t['id']}: {t['date']} {t['start']}-{t['end']}")
 if a.cmd=='preview': print('预览模式：不会访问预约接口、不会创建订单。')
if __name__=='__main__':main()
