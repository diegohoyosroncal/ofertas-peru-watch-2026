#!/usr/bin/env python3
"""Independent retail price watcher. No comparator scraping or credentials in source."""
import json, os, re, sys, urllib.request, urllib.robotparser, html
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit

ROOT=Path(__file__).parent
CONFIG=ROOT/'products.json'
STATE=ROOT/'history.json'
UA='OfertasPeruPriceWatch/1.0 (+https://github.com/diegohoyosroncal/ofertas-peru-watch-2026)'

class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__();self.metas={};self.ld=[];self.in_script=False;self.buf='';self.title='';self.h1='';self.in_title=False;self.in_h1=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='script' and a.get('type','').lower()=='application/ld+json':self.in_script=True;self.buf=''
        if tag=='title':self.in_title=True
        if tag=='h1':self.in_h1=True
        if tag=='meta':
            key=a.get('property') or a.get('name') or a.get('itemprop')
            if key and 'content' in a:self.metas[key.lower()]=a['content']
    def handle_endtag(self,tag):
        if tag=='script' and self.in_script:self.ld.append(self.buf);self.in_script=False
        if tag=='title':self.in_title=False
        if tag=='h1':self.in_h1=False
    def handle_data(self,data):
        if self.in_script:self.buf+=data
        if self.in_title:self.title+=data
        if self.in_h1:self.h1+=data

def number(v):
    if v is None:return None
    s=str(v).strip().replace('S/','').replace(' ','').replace('\xa0','')
    if ',' in s and '.' in s:s=s.replace(',','')
    elif ',' in s:
        parts=s.split(',');s=s.replace(',','.') if len(parts[-1])==2 else s.replace(',','')
    try:
        value=float(s)
        return round(value,2) if 1 <= value <= 100000 else None
    except ValueError:return None

def scan_jsonld(raw):
    try:root=json.loads(html.unescape(raw.strip()))
    except Exception:return []
    out=[]
    def visit(obj):
        if isinstance(obj,list):
            for x in obj:visit(x)
        if not isinstance(obj,dict):return
        types=obj.get('@type',[]);types=[types] if isinstance(types,str) else types
        if 'Product' in types:
            offers=obj.get('offers',[]);offers=[offers] if isinstance(offers,dict) else offers
            for o in offers:
                if isinstance(o,dict):
                    value=number(o.get('price') or o.get('lowPrice'))
                    if not value and isinstance(o.get('priceSpecification'),dict):value=number(o['priceSpecification'].get('price'))
                    if value:out.append({'price':value,'name':str(obj.get('name','')),'availability':str(o.get('availability',''))})
        if '@graph' in obj:visit(obj['@graph'])
    visit(root);return out

def parse_product(page,product):
    p=MetaParser();p.feed(page)
    name=html.unescape(p.h1 or p.metas.get('og:title','') or p.title)
    expected=[x.casefold() for x in product['must_contain']]
    if not all(x in name.casefold() for x in expected):raise ValueError('Mismatched product identity: '+name[:120])
    candidates=[]
    for raw in p.ld:
        for item in scan_jsonld(raw):
            if all(x in item['name'].casefold() for x in expected):candidates.append(item)
    if candidates:
        prices={x['price'] for x in candidates}
        if len(prices)==1:
            chosen=next(iter(prices))
        else:
            visible=html.unescape(re.sub(r'<[^>]+>',' ',page))
            discount=re.search(r'Precio\s+especial\s*S/\s*([\d,.]+)',visible,re.I)
            sale=number(discount.group(1)) if discount else None
            meta=number(p.metas.get('product:price:amount') or p.metas.get('og:price:amount'))
            chosen= sale if sale in prices else (meta if meta in prices else None)
            if chosen is None:raise ValueError('Ambiguous product offers, candidates='+str(sorted(prices))+' meta='+str(meta)+' sale='+str(sale))
        item=next(i for i in candidates if i['price']==chosen)
        a=item['availability'].lower()
        visible=html.unescape(re.sub(r'<[^>]+>',' ',page))
        stock=('out' not in a and ('instock' in a or 'limitedavailability' in a)) or bool(re.search(r'(Disponible.{0,35}(unidades|despacho)|En stock|Disponibilidad:\s*Disponible|[0-9]+ unidades disponibles|[0-9]+ disponibles)',visible,re.I))
        return {'price':item['price'],'name':name,'in_stock':stock,'source':'jsonld-validated'}
    for key in ['product:price:amount','og:price:amount','price']:
        v=number(p.metas.get(key))
        if v:
            stock=bool(re.search(r'Disponible.{0,25}(unidades|despacho)',page,re.I))
            return {'price':v,'name':name,'in_stock':stock,'source':'product-meta'}
    stripped=html.unescape(re.sub(r'<[^>]+>',' ',page))
    m=re.search(r'Precio\s+especial\s*S/\s*([\d,.]+)',stripped,re.I)
    if m:
        v=number(m.group(1))
        if v:return {'price':v,'name':name,'in_stock':bool(re.search(r'Disponible.{0,30}(unidades|despacho)',stripped,re.I)),'source':'visible-special-price'}
    raise ValueError('No reliable product price')

def get_page(url):
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.netloc not in {'hiraoka.com.pe','www.hiraoka.com.pe','memorykings.pe','www.memorykings.pe','carsa.pe','www.carsa.pe'}:
        raise ValueError('Unapproved merchant host')
    robots=parts.scheme+'://'+parts.netloc+'/robots.txt'
    rp=urllib.robotparser.RobotFileParser();rp.set_url(robots);rp.read()
    if not rp.can_fetch(UA,url):raise PermissionError('robots.txt disallows this URL')
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept-Language':'es-PE,es;q=0.9','Accept':'text/html'})
    with urllib.request.urlopen(req,timeout=14) as resp:
        if not resp.headers.get('Content-Type','').startswith('text/html'):raise ValueError('Not HTML')
        body=resp.read(1_500_001)
        if len(body)>1_500_000:raise ValueError('Page too large')
        return body.decode('utf-8','replace')

def api(method,path,obj=None):
    token=os.environ.get('GITHUB_TOKEN','');repo=os.environ.get('GITHUB_REPOSITORY','')
    if not token or not repo:raise ValueError('Missing GitHub runtime token/repository')
    payload=json.dumps(obj).encode() if obj is not None else None
    req=urllib.request.Request('https://api.github.com/repos/'+repo+path,data=payload,method=method,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':UA,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=14) as resp:return json.load(resp)

def alert(product, price, baseline, url, source):
    title='[OFERTA BF 2026] '+product['id']+' S/'+str(price)
    try:
        prior=api('GET','/issues?state=all&per_page=100')
        if any(title==i.get('title') for i in prior):return 'already-posted'
        pct=round(100*(baseline-price)/baseline,1)
        body=(f"Precio actual comprobado en la tienda: S/{price:.2f} para **{product['label']}**.\n"
              f"Referencia histórica anterior: S/{baseline:.2f}; diferencia: **{pct}%**.\n"
              f"Fuente histórica: {product.get('reference_url','serie propia 30 días')}.\n"
              f"Precio en tienda: {url}\nOrigen de extracción: {source}.\n"
              "Stock público indicado en la ficha. **Verificar entrega a Lima, garantía y precio final antes de pagar.**\n"
              "No se efectuó ninguna compra.")
        owner=os.environ.get('GITHUB_REPOSITORY','').partition('/')[0]
        if not owner:raise ValueError('Missing GitHub repository owner')
        api('POST','/issues',{'title':title,'body':body+'\n@'+owner,'assignees':[owner]})
        return 'issue-posted'
    except Exception as e:return 'alert-failed:'+str(e)[:140]

def run():
    now=datetime.now(timezone.utc)
    if now.year!=2026 or now.month!=11 or now.day<2 or now.day>30:
        print('out-of-season: no scans');return 0
    products=json.loads(CONFIG.read_text())
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    scanned=0;success=0;errors=[];posted=0
    for pr in products:
        scanned+=1
        try:
            observation=parse_product(get_page(pr['url']),pr)
            success+=1
            row=state.setdefault(pr['id'],{'observations':[]})
            day=now.date().isoformat()
            past=[e for e in row['observations'] if e['date']<day and e.get('price')]
            previous=[e['price'] for e in past if e['date']>=(now.date()-timedelta(days=30)).isoformat()]
            baseline=pr.get('fixed_pre_event_min') if pr.get('fixed_pre_event_min') else (min(previous) if len(previous)>=7 else None)
            row['observations']=[e for e in row['observations'] if e['date']!=day]
            row['observations'].append({'date':day,'price':observation['price'],'in_stock':observation['in_stock'],'source':observation['source']})
            row['observations']=[e for e in row['observations'] if e['date']>=(now.date()-timedelta(days=40)).isoformat()]
            notice='no-discount'
            if baseline and observation['in_stock'] and observation['price']<=baseline*0.85:
                notice=alert(pr,observation['price'],baseline,pr['url'],observation['source'])
                if notice=='issue-posted':posted+=1
            print(json.dumps({'id':pr['id'],'price':observation['price'],'stock':observation['in_stock'],'reference':baseline,'result':notice},ensure_ascii=False))
        except Exception as e:
            errors.append(pr['id']+':'+type(e).__name__+':'+str(e)[:140])
    STATE.write_text(json.dumps(state,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'attempted':scanned,'verified':success,'alerts_created':posted,'errors':errors},ensure_ascii=False))
    return 1 if success==0 else 0

def self_test():
    p={'must_contain':['galaxy a27','128gb']}
    good='<h1>Samsung Galaxy A27 128GB</h1><script type="application/ld+json">'+json.dumps({'@type':'Product','name':'Samsung Galaxy A27 128GB','offers':{'price':'949.00','availability':'https://schema.org/InStock'}})+'</script>'
    a=parse_product(good,p);assert a['price']==949 and a['in_stock']
    assert number('1,449.00')==1449 and number('949,00')==949
    try:parse_product(good,{'must_contain':['256gb']});raise AssertionError('must reject wrong SKU')
    except ValueError:pass
    assert scan_jsonld('{bad')==[]
    print('SELF_TEST_PASS parser, price normalization, SKU guard, malformed JSON-LD')

if __name__=='__main__':
    if '--self-test' in sys.argv:self_test()
    else:sys.exit(run())
