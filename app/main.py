from __future__ import annotations
import csv
import html
import io
import json
import math
import os
import re
import secrets
import unicodedata
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from .db import (ROOT, UPLOADS, PRODUCTION, IS_VERCEL, DEFAULTS, database, initialize, rows, site_settings,
                 DatabaseError, IntegrityError)
from .security import (COOKIE, digest, now, new_session, require_owner, checked_form,
                       limited, client_identity, password_valid, validate_password, password_hasher, audit)
from .media import save_image, delete_image, MAX_IMAGE_SIZE

CATEGORIES=['Earrings','Necklaces','Rings','Bracelets','Anklets','Sets','Other']
STATUS=['requested','confirmed','shipped','completed','cancelled']

def bootstrap_owner_from_env():
    """Create the first owner once from Vercel secrets; never overwrite an existing owner."""
    username=os.environ.get('JAGHVI_OWNER_ID','').strip()
    owner_email=os.environ.get('JAGHVI_OWNER_EMAIL','').strip().lower()
    password=os.environ.get('JAGHVI_OWNER_PASSWORD','')
    with database() as con:
        if con.execute('SELECT 1 FROM owners').fetchone():
            return
    if not (username and owner_email and password):
        return
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',username):
        raise RuntimeError('JAGHVI_OWNER_ID must use 3–64 letters, digits, dots, hyphens, or underscores.')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',owner_email):
        raise RuntimeError('JAGHVI_OWNER_EMAIL is not a valid email address.')
    validate_password(password)
    with database(True) as con:
        if not con.execute('SELECT 1 FROM owners').fetchone():
            con.execute('INSERT INTO owners(username,email,password_hash,created_at) VALUES (?,?,?,?)',
                        (username,owner_email,password_hasher.hash(password),now()))


@asynccontextmanager
async def lifespan(app):
    initialize()
    bootstrap_owner_from_env()
    yield

app=FastAPI(title='Jaghvi',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
default_hosts='localhost,127.0.0.1,testserver,*.vercel.app,jaghvi.com,*.jaghvi.com'
app.add_middleware(TrustedHostMiddleware,allowed_hosts=[x.strip() for x in os.environ.get('ALLOWED_HOSTS',default_hosts).split(',') if x.strip()])
STATIC_ROOT=ROOT/'app/static'
# Keep the static directory inside the FastAPI source tree and pass a literal path.
# Vercel can detect this mount at build time and include/promote the files correctly.
app.mount('/static',StaticFiles(directory='app/static'),name='static')
if not IS_VERCEL:
    UPLOADS.mkdir(parents=True,exist_ok=True)
    app.mount('/media',StaticFiles(directory=UPLOADS),name='media')
templates=Jinja2Templates(directory='app/templates')
templates.env.filters['money']=lambda v:f'{int(v)/100:,.2f}'.removesuffix('.00')
templates.env.filters['date']=lambda v:__import__('datetime').datetime.fromtimestamp(v,__import__('datetime').timezone.utc).strftime('%d %b %Y')

# Enforce a request-body limit even when Content-Length is absent or misleading.
class BodyLimitMiddleware:
    def __init__(self, app, limit=None): self.app,self.limit=app,(4*1024*1024 if IS_VERCEL else 32*1024*1024) if limit is None else limit
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['method'] in ('GET','HEAD','OPTIONS'):
            return await self.app(scope,receive,send)
        messages=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            size+=len(message.get('body',b''))
            if size>self.limit:
                return await Response('Upload limit exceeded. On Vercel, keep each form submission under 4 MB.',413)(scope,receive,send)
            messages.append(message)
            if not message.get('more_body',False): break
        async def replay():
            if messages: return messages.pop(0)
            return await receive()
        await self.app(scope,replay,send)
app.add_middleware(BodyLimitMiddleware)

@app.middleware('http')
async def session_and_headers(request,call_next):
    dynamic=not request.url.path.startswith(('/static/','/media/')) and request.url.path!='/health'
    if dynamic:
        token=request.cookies.get(COOKIE,'')
        with database() as con:
            row=con.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',(digest(token),now())).fetchone() if token else None
        if row:
            session=dict(row);request.state.new_cookie=None
        else:
            token,session=new_session();request.state.new_cookie=token
        request.state.session=session
        request.state.data=json.loads(session['data'])
        with database() as con:
            owner=con.execute('SELECT id,username,email FROM owners WHERE id=?',(session['owner_id'],)).fetchone()
            request.state.owner=dict(owner) if owner else None
    response=await call_next(request)
    if dynamic and request.state.new_cookie:
        response.set_cookie(COOKIE,request.state.new_cookie,httponly=True,secure=PRODUCTION,samesite='lax',
                            max_age=8*3600 if request.state.session['owner_id'] else 7*86400,path='/')
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://*.public.blob.vercel-storage.com; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    if PRODUCTION: response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    if dynamic: response.headers['Cache-Control']='private, no-store'
    if request.url.path.startswith('/owner'): response.headers['X-Robots-Tag']='noindex, nofollow'
    return response

def persist_session(request):
    with database(True) as con:
        con.execute('UPDATE sessions SET data=? WHERE token_hash=?',(json.dumps(request.state.data),request.state.session['token_hash']))

def redirect(path): return RedirectResponse(path,303)

def flash(request,text):
    request.state.data['flash']=text;persist_session(request)

def render(request,name,**context):
    with database() as con:
        site=site_settings(con)
        collections=rows(con.execute('SELECT * FROM collections WHERE visible=1 ORDER BY position,id'))
    data=request.state.data
    notice=data.pop('flash',None)
    if notice: persist_session(request)
    return templates.TemplateResponse(request=request,name=name,context={
        'site':site,'nav_collections':collections,'csrf':request.state.session['csrf'],
        'owner':request.state.owner,'cart_count':sum(int(v) for v in data.get('cart',{}).values()),
        'wish_count':len(data.get('wishlist',[])),'notice':notice,'path':request.url.path,
        'year':__import__('datetime').date.today().year,'categories':CATEGORIES,**context})

@app.exception_handler(HTTPException)
async def http_error(request,exc):
    if exc.status_code in (301,302,303,307): return redirect(exc.headers['Location'])
    if getattr(request.state,'session',None):
        result=render(request,'error.html',title='A moment, please.',error=exc.detail,status=exc.status_code)
        result.status_code=exc.status_code;return result
    return Response(str(exc.detail),exc.status_code)

@app.exception_handler(DatabaseError)
async def database_error(request,exc):
    import logging
    logging.getLogger('jaghvi').exception('Database operation failed')
    return Response('The studio is temporarily busy. Please retry in a moment.',503)

def clean_text(value,maxlen=5000):
    text=str(value or '').strip()
    if len(text)>maxlen: raise ValueError(f'Please keep this field within {maxlen} characters.')
    return text

def required(form,key,maxlen=200):
    value=clean_text(form.get(key),maxlen)
    if not value: raise ValueError(f'Please fill in {key.replace("_"," ")}.')
    return value

def integer(value,minimum=0,maximum=1000000):
    try:
        parsed=int(str(value))
        if not minimum<=parsed<=maximum: raise ValueError
        return parsed
    except (ValueError,TypeError): raise ValueError(f'Enter a whole number from {minimum} to {maximum}.')

def cents(value):
    try:
        d=Decimal(str(value))
        if not d.is_finite() or d<0 or d>10000000 or d.as_tuple().exponent < -2: raise ValueError
        return int(d*100)
    except (InvalidOperation,ValueError): raise ValueError('Enter a valid price with at most two decimal places.')

def email(value):
    value=clean_text(value,254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value): raise ValueError('Enter a valid email address.')
    return value

def slugify(value):
    value=unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+','-',value).strip('-')[:120] or secrets.token_hex(6)

PRODUCT_SELECT='''SELECT p.*, c.name AS collection_name,c.slug AS collection_slug,
 (SELECT path FROM images WHERE product_id=p.id ORDER BY position,id LIMIT 1) AS cover,
 (SELECT COUNT(*) FROM variants WHERE product_id=p.id) AS variant_count,
 COALESCE((SELECT MIN(price) FROM variants WHERE product_id=p.id),p.price) AS effective_price,
 COALESCE((SELECT SUM(stock) FROM variants WHERE product_id=p.id),p.stock) AS available
 FROM products p LEFT JOIN collections c ON c.id=p.collection_id'''

def get_product(con,pid):
    row=con.execute(PRODUCT_SELECT+' WHERE p.id=?',(pid,)).fetchone()
    if not row: raise HTTPException(404,'This piece could not be found.')
    p=dict(row)
    p['images']=rows(con.execute('SELECT * FROM images WHERE product_id=? ORDER BY position,id',(pid,)))
    p['variants']=rows(con.execute('SELECT * FROM variants WHERE product_id=? ORDER BY id',(pid,)))
    return p

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/')
def home(request:Request):
    with database() as con:
        site=site_settings(con)
        featured=rows(con.execute(PRODUCT_SELECT+" WHERE p.status='published' AND p.featured=1 ORDER BY p.updated_at DESC LIMIT 4"))
        if not featured:
            featured=rows(con.execute(PRODUCT_SELECT+" WHERE p.status='published' ORDER BY p.created_at DESC LIMIT 4"))
    return render(request,'home.html',featured=featured,sections=site['section_order'].split(','))

@app.get('/shop')
def shop(request:Request,q:str='',collection:str='',category:str='',sort:str='newest',page:int=1):
    q=q[:100];collection=collection[:120];category=category[:60]
    where=["p.status='published'"];params=[]
    if q:
        where.append('(p.name LIKE ? OR p.description LIKE ? OR p.material LIKE ?)');params.extend(['%'+q+'%']*3)
    if collection: where.append('c.slug=?');params.append(collection)
    if category: where.append('p.category=?');params.append(category)
    order={'newest':'p.created_at DESC','price-low':'effective_price ASC','price-high':'effective_price DESC','name':'p.name ASC'}.get(sort,'p.created_at DESC')
    with database() as con:
        count=con.execute('SELECT COUNT(*) FROM products p LEFT JOIN collections c ON c.id=p.collection_id WHERE '+' AND '.join(where),params).fetchone()[0]
        pages=max(1,math.ceil(count/12));page=max(1,min(page,pages))
        products=rows(con.execute(PRODUCT_SELECT+' WHERE '+' AND '.join(where)+' ORDER BY '+order+' LIMIT 12 OFFSET ?',params+[(page-1)*12]))
        current=con.execute('SELECT * FROM collections WHERE slug=?',(collection,)).fetchone() if collection else None
    return render(request,'shop.html',products=products,count=count,current=dict(current) if current else None,
                  q=q,collection=collection,category=category,sort=sort,page=page,pages=pages,
                  pagination_query=urlencode({'q':q,'collection':collection,'category':category,'sort':sort}))

@app.get('/pieces/{slug}')
def product_detail(request:Request,slug:str):
    with database() as con:
        row=con.execute("SELECT id FROM products WHERE slug=? AND status='published'",(slug,)).fetchone()
        if not row: raise HTTPException(404,'This piece is not currently available.')
        product=get_product(con,row['id'])
    return render(request,'product.html',product=product,title=product['name']+' | Jaghvi')

@app.get('/story')
def story(request:Request): return render(request,'story.html',title='Our story | Jaghvi')

@app.get('/policies/{policy}')
def policy_page(request:Request,policy:str):
    labels={'privacy':'Privacy','shipping':'Shipping & delivery','returns':'Returns & exchanges','terms':'Terms of service'}
    if policy not in labels: raise HTTPException(404,'This page does not exist.')
    with database() as con: text=site_settings(con)[policy+'_policy']
    return render(request,'policy.html',title=labels[policy],text=text)

@app.get('/contact')
def contact(request:Request): return render(request,'contact.html',title='Contact Jaghvi')

@app.post('/contact')
async def send_contact(request:Request):
    form=await checked_form(request)
    if limited('contact',client_identity(request),5,3600): raise HTTPException(429,'Please wait before sending another message.')
    try:
        with database(True) as con:
            site=site_settings(con)
            if not site['privacy_policy']: raise ValueError('Contact messaging will open when our privacy information is published.')
            if not form.get('consent'): raise ValueError('Please acknowledge the privacy policy.')
            con.execute('INSERT INTO messages(name,email,message,created_at) VALUES (?,?,?,?)',
                        (required(form,'name',120),email(form.get('email')),required(form,'message',5000),now()))
    except ValueError as exc: return render(request,'contact.html',error=str(exc),form=form)
    flash(request,'Your message is with the Jaghvi team. Thank you for reaching out.')
    return redirect('/contact')

@app.post('/subscribe')
async def subscribe(request:Request):
    form=await checked_form(request)
    if limited('subscribe',client_identity(request),5,3600): raise HTTPException(429,'Please wait before trying again.')
    try:
        with database(True) as con:
            site=site_settings(con)
            if not site['newsletter_enabled'] or not site['privacy_policy']: raise ValueError('Sign-ups are not open yet.')
            if not form.get('consent'): raise ValueError('Please consent to receiving collection updates.')
            con.execute('INSERT OR IGNORE INTO subscribers(email,consent_text,created_at) VALUES (?,?,?)',
                        (email(form.get('email')),'I agree to receive Jaghvi collection updates and have read the privacy policy.',now()))
    except ValueError as exc: raise HTTPException(400,str(exc))
    flash(request,'You are on the list. Welcome to the world of Jaghvi.')
    return redirect('/#newsletter')

def cart_items(request,con):
    items=[];invalid=[]
    for key,quantity in request.state.data.get('cart',{}).items():
        try:
            pid,vid=key.split(':');pid=int(pid);vid=int(vid)
            p=get_product(con,pid)
            if p['status']!='published': invalid.append(key);continue
            v=con.execute('SELECT * FROM variants WHERE id=? AND product_id=?',(vid,pid)).fetchone() if vid else None
            if (vid and not v) or (p['variant_count'] and not v): invalid.append(key);continue
            price=v['price'] if v else p['price'];stock=v['stock'] if v else p['stock']
            quantity=integer(quantity,1,20)
            items.append({'key':key,'product':p,'variant':dict(v) if v else None,'quantity':quantity,
                          'price':price,'stock':stock,'total':price*quantity})
        except (ValueError,HTTPException): invalid.append(key)
    return items,invalid

@app.get('/bag')
def bag(request:Request):
    with database() as con: items,invalid=cart_items(request,con)
    if invalid:
        for k in invalid: request.state.data.get('cart',{}).pop(k,None)
        persist_session(request)
    return render(request,'bag.html',items=items,subtotal=sum(i['total'] for i in items),title='Your bag | Jaghvi')

@app.post('/bag/add')
async def bag_add(request:Request):
    form=await checked_form(request)
    try:
        pid=integer(form.get('product_id'),1);vid=integer(form.get('variant_id') or 0);qty=integer(form.get('quantity',1),1,20)
        with database() as con:
            p=get_product(con,pid)
            if p['status']!='published': raise ValueError('This piece is not available.')
            v=con.execute('SELECT * FROM variants WHERE id=? AND product_id=?',(vid,pid)).fetchone() if vid else None
            if (p['variant_count'] and not v) or (vid and not v): raise ValueError('Choose an available size or variation.')
            stock=v['stock'] if v else p['stock']
        cart=request.state.data.setdefault('cart',{});key=f'{pid}:{vid}'
        total=cart.get(key,0)+qty
        if total>stock or total>20: raise ValueError('That quantity is not currently available.')
        cart[key]=total;persist_session(request)
    except ValueError as exc: raise HTTPException(400,str(exc))
    flash(request,'Added to your bag.');return redirect('/bag')

@app.post('/bag/update')
async def bag_update(request:Request):
    form=await checked_form(request);key=str(form.get('key',''))
    cart=request.state.data.setdefault('cart',{})
    if key not in cart: return redirect('/bag')
    try: qty=integer(form.get('quantity',0),0,20)
    except ValueError as exc: raise HTTPException(400,str(exc))
    if qty==0: cart.pop(key,None)
    else: cart[key]=qty
    persist_session(request);return redirect('/bag')

@app.get('/wishlist')
def wishlist(request:Request):
    ids=request.state.data.get('wishlist',[])[:100]
    with database() as con:
        products=rows(con.execute(PRODUCT_SELECT+" WHERE p.status='published' AND p.id IN ("+','.join('?' for _ in ids)+')',ids)) if ids else []
    return render(request,'wishlist.html',products=products,title='Saved pieces | Jaghvi')

@app.post('/wishlist')
async def wishlist_toggle(request:Request):
    form=await checked_form(request)
    try: pid=integer(form.get('product_id'),1)
    except ValueError as exc: raise HTTPException(400,str(exc))
    with database() as con:
        if get_product(con,pid)['status']!='published': raise HTTPException(404,'This piece is unavailable.')
    items=request.state.data.setdefault('wishlist',[])
    if pid in items: items.remove(pid)
    elif len(items)<100: items.append(pid)
    persist_session(request);return redirect('/wishlist')

@app.get('/checkout')
def checkout(request:Request):
    with database() as con:
        site=site_settings(con);items,invalid=cart_items(request,con)
    if not items: return redirect('/bag')
    request.state.data['checkout_nonce']=secrets.token_urlsafe(24);persist_session(request)
    return render(request,'checkout.html',items=items,subtotal=sum(i['total'] for i in items),
                  shipping=cents(site['shipping_fee']),nonce=request.state.data['checkout_nonce'],form={},title='Order request | Jaghvi')

@app.post('/checkout')
async def place_order(request:Request):
    form=await checked_form(request)
    if limited('checkout',client_identity(request),10,3600): raise HTTPException(429,'Please wait before sending another request.')
    if not secrets.compare_digest(str(form.get('nonce','')),str(request.state.data.get('checkout_nonce','none'))):
        raise HTTPException(409,'This order form has already been used or expired. Return to your bag to start again.')
    try:
        with database(True) as con:
            checkout_key=digest(request.state.session['token_hash']+str(form.get('nonce','')))
            prior=con.execute('SELECT reference FROM orders WHERE checkout_key=?',(checkout_key,)).fetchone()
            if prior: return redirect('/order/'+prior['reference'])
            site=site_settings(con)
            if not site['order_requests_enabled'] or not all(site[k] for k in ['privacy_policy','terms_policy','shipping_policy','returns_policy','contact_email']):
                raise ValueError('Online order requests are not open yet. Please check back soon.')
            if not form.get('consent'): raise ValueError('Please agree to the terms and privacy policy.')
            items,invalid=cart_items(request,con)
            if not items or invalid: raise ValueError('Your bag changed. Please review it before continuing.')
            if any(i['quantity']>i['stock'] for i in items): raise ValueError('An item has insufficient stock. Please update your bag.')
            subtotal=sum(i['total'] for i in items);shipping=cents(site['shipping_fee'])
            reference='JV-'+secrets.token_hex(5).upper()
            oid=con.execute('''INSERT INTO orders(reference,session_hash,checkout_key,name,email,phone,address,city,region,postal_code,country,note,subtotal,shipping,total,currency,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id''',
               (reference,request.state.session['token_hash'],checkout_key,required(form,'name',120),email(form.get('email')),
                required(form,'phone',40),required(form,'address',500),required(form,'city',100),required(form,'region',100),
                required(form,'postal_code',20),required(form,'country',80),clean_text(form.get('note'),2000),
                subtotal,shipping,subtotal+shipping,site['currency'],now())).fetchone()[0]
            for i in items:
                p=i['product'];v=i['variant']
                con.execute('INSERT INTO order_items(order_id,product_id,variant_id,name,variant,sku,quantity,price) VALUES (?,?,?,?,?,?,?,?)',
                            (oid,p['id'],v['id'] if v else None,p['name'],v['label'] if v else '',v['sku'] if v else p['sku'],i['quantity'],i['price']))
    except ValueError as exc:
        with database() as con: site=site_settings(con);items,_=cart_items(request,con)
        return render(request,'checkout.html',error=str(exc),form=form,items=items,subtotal=sum(i['total'] for i in items),shipping=cents(site['shipping_fee']),nonce=form.get('nonce',''))
    request.state.data.pop('checkout_nonce',None);request.state.data['cart']={};persist_session(request)
    return redirect('/order/'+reference)

@app.get('/order/{reference}')
def order_received(request:Request,reference:str):
    with database() as con:
        order=con.execute('SELECT * FROM orders WHERE reference=? AND session_hash=?',(reference,request.state.session['token_hash'])).fetchone()
    if not order: raise HTTPException(404,'This request is unavailable in this browser session.')
    return render(request,'order_received.html',order=dict(order),title='Request received | Jaghvi')

@app.get('/robots.txt')
def robots(request:Request):
    return Response('User-agent: *\nAllow: /\nDisallow: /owner\nDisallow: /bag\nDisallow: /checkout\nDisallow: /wishlist\nDisallow: /order/\nSitemap: '+str(request.base_url)+'sitemap.xml\n',media_type='text/plain')

@app.get('/sitemap.xml')
def sitemap(request:Request):
    with database() as con: slugs=[r[0] for r in con.execute("SELECT slug FROM products WHERE status='published'")]
    paths=['','shop','story','contact']+['pieces/'+s for s in slugs]
    urls=''.join('<url><loc>'+html.escape(str(request.base_url)+p)+'</loc></url>' for p in paths)
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+urls+'</urlset>',media_type='application/xml')

# ----------------------- Private owner studio -----------------------
@app.get('/owner/login')
def owner_login(request:Request):
    if request.state.owner: return redirect('/owner')
    with database() as con: configured=bool(con.execute('SELECT 1 FROM owners').fetchone())
    return render(request,'owner/login.html',configured=configured,title='Owner sign in | Jaghvi')

@app.post('/owner/login')
async def owner_authenticate(request:Request):
    form=await checked_form(request)
    username=str(form.get('username','')).strip()[:120]
    ip=client_identity(request)
    account=digest(username.casefold())
    if limited('login-ip',ip,30,900) or limited('login-account',account,8,900):
        result=render(request,'owner/login.html',configured=True,error='Too many attempts. Please wait 15 minutes before trying again.')
        result.status_code=429;return result
    with database() as con:
        row=con.execute('SELECT * FROM owners WHERE username=? COLLATE NOCASE',(username,)).fetchone()
    valid=password_valid(str(form.get('password',''))[:256],row['password_hash'] if row else None)
    if not row or not valid:
        result=render(request,'owner/login.html',configured=True,error='The master ID or password is incorrect.')
        result.status_code=401;return result
    token,session=new_session(row['id'])
    with database(True) as con:
        con.execute('DELETE FROM sessions WHERE token_hash=?',(request.state.session['token_hash'],))
        con.execute("DELETE FROM attempts WHERE (kind='login-account' AND identity=?) OR (kind='login-ip' AND identity=?)",(account,ip))
        audit(con,row['id'],'Owner signed in')
    request.state.session=session;request.state.new_cookie=token
    return redirect('/owner')

@app.post('/owner/logout')
async def owner_logout(request:Request):
    require_owner(request);await checked_form(request)
    with database(True) as con: con.execute('DELETE FROM sessions WHERE token_hash=?',(request.state.session['token_hash'],))
    token,session=new_session();request.state.new_cookie=token;request.state.session=session
    return redirect('/owner/login')

@app.get('/owner')
def owner_dashboard(request:Request):
    require_owner(request)
    with database() as con:
        stats={
            'published':con.execute("SELECT COUNT(*) FROM products WHERE status='published'").fetchone()[0],
            'drafts':con.execute("SELECT COUNT(*) FROM products WHERE status='draft'").fetchone()[0],
            'requests':con.execute("SELECT COUNT(*) FROM orders WHERE status='requested'").fetchone()[0],
            'subscribers':con.execute('SELECT COUNT(*) FROM subscribers').fetchone()[0],
            'images':con.execute('SELECT COUNT(*) FROM images').fetchone()[0]}
        recent=rows(con.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 5'))
        events=rows(con.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 5'))
    return render(request,'owner/dashboard.html',stats=stats,recent=recent,events=events,active='overview',title='Your studio | Jaghvi')

@app.get('/owner/products')
def owner_products(request:Request,q:str='',status:str='all',page:int=1):
    require_owner(request);where=['1=1'];params=[]
    if q: where.append('(p.name LIKE ? OR p.sku LIKE ?)');params.extend(['%'+q[:100]+'%']*2)
    if status in ['draft','published','archived']:where.append('p.status=?');params.append(status)
    with database() as con:
        count=con.execute('SELECT COUNT(*) FROM products p WHERE '+' AND '.join(where),params).fetchone()[0]
        pages=max(1,math.ceil(count/40));page=max(1,min(page,pages))
        products=rows(con.execute(PRODUCT_SELECT+' WHERE '+' AND '.join(where)+' ORDER BY p.updated_at DESC LIMIT 40 OFFSET ?',params+[(page-1)*40]))
    return render(request,'owner/products.html',products=products,q=q,status=status,count=count,page=page,pages=pages,
                  pagination_query=urlencode({'q':q,'status':status}),active='products',title='Products | Jaghvi studio')

def product_editor(request,product=None,error=None):
    with database() as con: collections=rows(con.execute('SELECT * FROM collections ORDER BY position,id'))
    return render(request,'owner/product_edit.html',product=product or {'status':'draft','stock':0,'price':0,'images':[],'variants':[]},
                  collections=collections,error=error,active='products',title='Edit piece | Jaghvi studio')

@app.get('/owner/products/new')
def owner_product_new(request:Request):
    require_owner(request);return product_editor(request)

@app.get('/owner/products/{pid:int}')
def owner_product_edit(request:Request,pid:int):
    require_owner(request)
    with database() as con: p=get_product(con,pid)
    return product_editor(request,p)

async def uploaded_images(form,key):
    saved=[]
    try:
        files=[f for f in form.getlist(key) if getattr(f,'filename','')]
        if len(files)>12: raise ValueError('Upload no more than 12 images at a time.')
        for upload in files:
            raw=await upload.read(MAX_IMAGE_SIZE+1)
            saved.append(await run_in_threadpool(save_image,raw,upload.filename))
        return saved
    except Exception:
        for path in saved: delete_image(path)
        raise

@app.post('/owner/products/new')
@app.post('/owner/products/{pid:int}')
async def owner_product_save(request:Request,pid:int|None=None):
    owner=require_owner(request);form=await checked_form(request);saved=[]
    try:
        name=required(form,'name',160);slug=slugify(form.get('slug') or name)
        price=cents(form.get('price') or '0');stock=integer(form.get('stock',0))
        status=str(form.get('status','draft'))
        if status not in ['draft','published','archived']: raise ValueError('Choose a valid product status.')
        category=str(form.get('category','Other'))
        if category not in CATEGORIES: raise ValueError('Choose a valid jewelry category.')
        material=clean_text(form.get('material'),500)
        if status=='published' and (price<=0 or not material):
            raise ValueError('A published piece needs a positive price and an accurate material description.')
        saved=await uploaded_images(form,'photos')
        with database(True) as con:
            existing=get_product(con,pid) if pid else None
            images_count=len(existing['images']) if existing else 0
            if images_count+len(saved)>12: raise ValueError('A piece can have at most 12 images. Remove an existing image first.')
            if status=='published' and images_count+len(saved)==0:
                raise ValueError('Add at least one real product photograph before publishing. You can save a draft without photos.')
            cid=integer(form.get('collection_id'),1) if form.get('collection_id') else None
            if cid and not con.execute('SELECT 1 FROM collections WHERE id=?',(cid,)).fetchone(): raise ValueError('Choose an existing collection.')
            values=(name,slug,clean_text(form.get('sku'),80),clean_text(form.get('description'),12000),material,category,cid,
                    price,stock,status,int(bool(form.get('featured'))),clean_text(form.get('care'),4000),clean_text(form.get('dimensions'),500),now())
            if pid:
                con.execute('''UPDATE products SET name=?,slug=?,sku=?,description=?,material=?,category=?,collection_id=?,price=?,stock=?,status=?,featured=?,care=?,dimensions=?,updated_at=? WHERE id=?''',values+(pid,))
            else:
                pid=con.execute('''INSERT INTO products(name,slug,sku,description,material,category,collection_id,price,stock,status,featured,care,dimensions,updated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id''',values+(now(),)).fetchone()[0]
            for index,path in enumerate(saved):
                con.execute('INSERT INTO images(product_id,path,alt,position) VALUES (?,?,?,?)',(pid,path,name,images_count+index))
            if existing:
                for image in existing['images']:
                    con.execute('UPDATE images SET alt=?,position=? WHERE id=? AND product_id=?',
                                (clean_text(form.get('alt_'+str(image['id']),image['alt']),300),integer(form.get('position_'+str(image['id']),image['position']),0,100),image['id'],pid))
            audit(con,owner['id'],'Saved product: '+name)
    except (ValueError,IntegrityError) as exc:
        for path in saved: delete_image(path)
        error='This URL is already used by another piece. Choose a different URL slug.' if isinstance(exc,IntegrityError) else str(exc)
        with database() as con: p=get_product(con,pid) if pid and con.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone() else {}
        p.update({k:str(v) for k,v in form.items() if not hasattr(v,'filename')})
        try: p['price']=cents(form.get('price') or '0')
        except ValueError: p['price']=0
        p.setdefault('images',[]);p.setdefault('variants',[])
        return product_editor(request,p,error)
    flash(request,'Your piece has been saved'+(' and published.' if status=='published' else ' as '+status+'.'))
    return redirect('/owner/products/'+str(pid))

@app.post('/owner/products/{pid:int}/archive')
async def owner_product_archive(request:Request,pid:int):
    owner=require_owner(request);await checked_form(request)
    with database(True) as con:
        p=get_product(con,pid);con.execute("UPDATE products SET status='archived',updated_at=? WHERE id=?",(now(),pid));audit(con,owner['id'],'Archived product: '+p['name'])
    flash(request,'Piece archived. Its order history is preserved.');return redirect('/owner/products')

@app.post('/owner/products/{pid:int}/images/{image_id:int}/remove')
async def owner_image_remove(request:Request,pid:int,image_id:int):
    owner=require_owner(request);await checked_form(request)
    with database(True) as con:
        image=con.execute('SELECT * FROM images WHERE id=? AND product_id=?',(image_id,pid)).fetchone()
        if not image: raise HTTPException(404,'Image not found.')
        image_path=image['path'];con.execute('DELETE FROM images WHERE id=?',(image_id,))
        if not con.execute('SELECT 1 FROM images WHERE product_id=?',(pid,)).fetchone():
            con.execute("UPDATE products SET status='draft' WHERE id=? AND status='published'",(pid,))
        audit(con,owner['id'],f'Removed image from product #{pid}')
    delete_image(image_path)
    flash(request,'Image removed. A piece without photos is kept as a draft.');return redirect('/owner/products/'+str(pid))

@app.post('/owner/products/{pid:int}/variants')
async def owner_variant_save(request:Request,pid:int):
    owner=require_owner(request);form=await checked_form(request)
    try:
        label=required(form,'label',100);price=cents(form.get('price'))
        if price<=0: raise ValueError('A variation needs a positive price.')
        stock=integer(form.get('stock',0));sku=clean_text(form.get('sku'),80)
        vid=integer(form.get('variant_id'),1) if form.get('variant_id') else None
        with database(True) as con:
            get_product(con,pid)
            if vid:
                result=con.execute('UPDATE variants SET label=?,price=?,stock=?,sku=? WHERE id=? AND product_id=?',(label,price,stock,sku,vid,pid))
                if not result.rowcount: raise ValueError('Variation not found.')
            else:
                if con.execute('SELECT COUNT(*) FROM variants WHERE product_id=?',(pid,)).fetchone()[0]>=30: raise ValueError('A piece can have up to 30 variations.')
                con.execute('INSERT INTO variants(product_id,label,price,stock,sku) VALUES (?,?,?,?,?)',(pid,label,price,stock,sku))
            audit(con,owner['id'],f'Saved variation on product #{pid}')
    except (ValueError,IntegrityError) as exc:
        flash(request,str(exc) if isinstance(exc,ValueError) else 'This variation name is already in use.')
        return redirect('/owner/products/'+str(pid)+'#variations')
    flash(request,'Variation saved. When variations exist, their stock and prices are used.');return redirect('/owner/products/'+str(pid)+'#variations')

@app.post('/owner/products/{pid:int}/variants/{vid:int}/remove')
async def owner_variant_remove(request:Request,pid:int,vid:int):
    owner=require_owner(request);await checked_form(request)
    with database(True) as con:
        if con.execute("SELECT 1 FROM order_items i JOIN orders o ON o.id=i.order_id WHERE i.variant_id=? AND o.status IN ('requested','confirmed','shipped')",(vid,)).fetchone():
            raise HTTPException(409,'This variation is part of an open order. Complete or cancel that order first.')
        con.execute('DELETE FROM variants WHERE id=? AND product_id=?',(vid,pid))
        audit(con,owner['id'],f'Removed variation from product #{pid}')
    return redirect('/owner/products/'+str(pid)+'#variations')

@app.get('/owner/collections')
def owner_collections(request:Request):
    require_owner(request)
    with database() as con:
        collections=rows(con.execute('SELECT c.*,(SELECT COUNT(*) FROM products WHERE collection_id=c.id) AS product_count FROM collections c ORDER BY position,id'))
    return render(request,'owner/collections.html',collections=collections,active='collections',title='Collections | Jaghvi studio')

@app.post('/owner/collections')
async def owner_collection_save(request:Request):
    owner=require_owner(request);form=await checked_form(request);saved=[]
    try:
        name=required(form,'name',160);slug=slugify(form.get('slug') or name)
        cid=integer(form.get('id'),1) if form.get('id') else None
        saved=await uploaded_images(form,'photo')
        with database(True) as con:
            old=con.execute('SELECT * FROM collections WHERE id=?',(cid,)).fetchone() if cid else None
            if cid and not old: raise ValueError('Collection not found.')
            image=saved[0] if saved else (old['image'] if old else '')
            if form.get('remove_image'): image=''
            values=(name,slug,clean_text(form.get('description'),1000),image,integer(form.get('position',0),0,100),int(bool(form.get('visible'))))
            if cid: con.execute('UPDATE collections SET name=?,slug=?,description=?,image=?,position=?,visible=? WHERE id=?',values+(cid,))
            else: con.execute('INSERT INTO collections(name,slug,description,image,position,visible) VALUES (?,?,?,?,?,?)',values)
            audit(con,owner['id'],'Saved collection: '+name)
    except (ValueError,IntegrityError) as exc:
        for path in saved: delete_image(path)
        flash(request,str(exc) if isinstance(exc,ValueError) else 'That collection URL is already in use.');return redirect('/owner/collections')
    if old and old['image'] and old['image']!=image: delete_image(old['image'])
    for unused in saved[1:]: delete_image(unused)
    flash(request,'Collection saved.');return redirect('/owner/collections')

@app.get('/owner/storefront')
def owner_storefront(request:Request):
    require_owner(request);return render(request,'owner/storefront.html',active='storefront',title='Storefront | Jaghvi studio')

@app.post('/owner/storefront')
async def owner_storefront_save(request:Request):
    owner=require_owner(request);form=await checked_form(request);new_files=[]
    try:
        with database() as con: current=site_settings(con)
        updated=current.copy()
        for key,default in DEFAULTS.items():
            if key in ['logo','hero_image','story_image','currency_symbol']: continue
            if isinstance(default,bool): updated[key]=bool(form.get(key))
            elif key in form: updated[key]=clean_text(form.get(key),15000 if key.endswith('_policy') else 12000)
        for key in ['background_color','text_color','accent_color']:
            if not re.fullmatch(r'#[0-9a-fA-F]{6}',updated[key]): raise ValueError('Use six-digit hexadecimal colors.')
        if not updated['brand_name'] or not updated['hero_title']: raise ValueError('The brand name and main headline cannot be empty.')
        if updated['contact_email']: updated['contact_email']=email(updated['contact_email'])
        if updated['instagram_url'] and not re.fullmatch(r'https://[^\s<>]+',updated['instagram_url']): raise ValueError('Use a complete https:// social-profile address.')
        currencies={'INR':'₹','USD':'$','GBP':'£','EUR':'€','AED':'AED'}
        if updated['currency'] not in currencies: raise ValueError('Choose a supported currency.')
        updated['currency_symbol']=currencies[updated['currency']]
        if updated['type_style'] not in ['editorial','modern']: raise ValueError('Choose an available typography style.')
        updated['shipping_fee']=str(Decimal(cents(updated['shipping_fee']))/100)
        section_order=[s.strip() for s in updated['section_order'].split(',')]
        if sorted(section_order)!=['collections','featured','newsletter','story']: raise ValueError('Section order must contain collections, featured, story, newsletter exactly once.')
        updated['section_order']=','.join(section_order)
        if updated['newsletter_enabled'] and not updated['privacy_policy']:
            raise ValueError('Publish your privacy policy before enabling newsletter sign-ups.')
        if updated['order_requests_enabled'] and not all(updated[k] for k in ['contact_email','privacy_policy','terms_policy','returns_policy','shipping_policy']):
            raise ValueError('Add your contact email and all four policies before enabling order requests.')
        with database() as con:
            if updated['currency']!=current['currency'] and con.execute('SELECT 1 FROM products').fetchone():
                raise ValueError('Set currency before adding products. Existing product prices cannot be relabelled as another currency.')
            if updated['order_requests_enabled'] and not con.execute("SELECT 1 FROM products WHERE status='published'").fetchone():
                raise ValueError('Publish your first real product before opening order requests.')
        for key in ['logo','hero_image','story_image']:
            saved=await uploaded_images(form,key+'_file');new_files+=saved
            if saved: updated[key]=saved[0]
            if form.get(key+'_reset'): updated[key]=DEFAULTS[key]
        with database(True) as con:
            for key,value in updated.items():
                con.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))
            audit(con,owner['id'],'Updated storefront content and settings')
    except ValueError as exc:
        for path in new_files: delete_image(path)
        flash(request,str(exc));return redirect('/owner/storefront')
    for key in ['logo','hero_image','story_image']:
        if current[key]!=updated[key]: delete_image(current[key])
    for path in new_files:
        if path not in updated.values(): delete_image(path)
    flash(request,'Your storefront changes are saved.');return redirect('/owner/storefront')

@app.get('/owner/orders')
def owner_orders(request:Request,status:str='all',page:int=1):
    require_owner(request);where='';params=[]
    if status in STATUS: where=' WHERE status=?';params=[status]
    with database() as con:
        count=con.execute('SELECT COUNT(*) FROM orders'+where,params).fetchone()[0]
        pages=max(1,math.ceil(count/40));page=max(1,min(page,pages))
        orders=rows(con.execute('SELECT * FROM orders'+where+' ORDER BY created_at DESC LIMIT 40 OFFSET ?',params+[(page-1)*40]))
    return render(request,'owner/orders.html',orders=orders,status=status,statuses=STATUS,page=page,pages=pages,
                  pagination_query=urlencode({'status':status}),active='orders',title='Order requests | Jaghvi studio')

@app.get('/owner/orders/{oid:int}')
def owner_order_detail(request:Request,oid:int):
    require_owner(request)
    with database() as con:
        order=con.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not order: raise HTTPException(404,'Order request not found.')
        items=rows(con.execute('SELECT * FROM order_items WHERE order_id=?',(oid,)))
    return render(request,'owner/order.html',order=dict(order),items=items,statuses=STATUS,active='orders',title='Order request | Jaghvi studio')

@app.post('/owner/orders/{oid:int}')
async def owner_order_update(request:Request,oid:int):
    owner=require_owner(request);form=await checked_form(request);status=str(form.get('status',''))
    transitions={'requested':['requested','confirmed','cancelled'],'confirmed':['confirmed','shipped','cancelled'],
                 'shipped':['shipped','completed'],'completed':['completed'],'cancelled':['cancelled']}
    try:
        with database(True) as con:
            order=con.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if not order: raise ValueError('Order not found.')
            if status not in transitions.get(order['status'],[]): raise ValueError('That status change is not allowed. Confirm before shipping; complete after shipping.')
            items=rows(con.execute('SELECT * FROM order_items WHERE order_id=?',(oid,)))
            reserved=order['inventory_reserved']
            if status=='confirmed' and not reserved:
                for item in items:
                    table='variants' if item['variant_id'] else 'products';itemid=item['variant_id'] or item['product_id']
                    if not itemid: raise ValueError('A requested item no longer exists. Cancel this request and contact the customer.')
                    result=con.execute(f'UPDATE {table} SET stock=stock-? WHERE id=? AND stock>=?',(item['quantity'],itemid,item['quantity']))
                    if not result.rowcount: raise ValueError('Insufficient stock to confirm this request. Update inventory or contact the customer.')
                reserved=1
            if status=='cancelled' and reserved:
                for item in items:
                    table='variants' if item['variant_id'] else 'products';itemid=item['variant_id'] or item['product_id']
                    con.execute(f'UPDATE {table} SET stock=stock+? WHERE id=?',(item['quantity'],itemid))
                reserved=0
            con.execute('UPDATE orders SET status=?,tracking=?,inventory_reserved=? WHERE id=?',
                        (status,clean_text(form.get('tracking'),500),reserved,oid))
            audit(con,owner['id'],'Updated '+order['reference']+' to '+status)
    except ValueError as exc: flash(request,str(exc));return redirect('/owner/orders/'+str(oid))
    flash(request,'Order request updated. No payment was collected by this website.');return redirect('/owner/orders/'+str(oid))

@app.get('/owner/inbox')
def owner_inbox(request:Request):
    require_owner(request)
    with database() as con: items=rows(con.execute('SELECT * FROM messages ORDER BY created_at DESC LIMIT 200'))
    return render(request,'owner/inbox.html',items=items,active='inbox',title='Inbox | Jaghvi studio')

@app.post('/owner/inbox/{mid:int}')
async def owner_message_action(request:Request,mid:int):
    owner=require_owner(request);form=await checked_form(request)
    with database(True) as con:
        if form.get('action')=='delete': con.execute('DELETE FROM messages WHERE id=?',(mid,));audit(con,owner['id'],'Deleted a contact message')
        else: con.execute('UPDATE messages SET is_read=1 WHERE id=?',(mid,))
    return redirect('/owner/inbox')

@app.get('/owner/subscribers')
def owner_subscribers(request:Request):
    require_owner(request)
    with database() as con: subscribers=rows(con.execute('SELECT * FROM subscribers ORDER BY created_at DESC LIMIT 1000'))
    return render(request,'owner/subscribers.html',subscribers=subscribers,active='subscribers',title='Subscribers | Jaghvi studio')

@app.post('/owner/subscribers/{sid:int}/remove')
async def owner_subscriber_remove(request:Request,sid:int):
    owner=require_owner(request);await checked_form(request)
    with database(True) as con:
        con.execute('DELETE FROM subscribers WHERE id=?',(sid,));audit(con,owner['id'],'Removed a newsletter subscriber')
    flash(request,'Subscriber removed.');return redirect('/owner/subscribers')

@app.get('/owner/export/{kind}')
def export_data(request:Request,kind:str):
    require_owner(request)
    queries={
        'subscribers':('SELECT email,consent_text,created_at FROM subscribers ORDER BY id',['email','consent_text','created_at']),
        'products':('SELECT name,slug,sku,description,material,category,price,stock,status FROM products ORDER BY id',['name','slug','sku','description','material','category','price_minor_units','stock','status']),
        'orders':('SELECT reference,name,email,phone,address,city,region,postal_code,country,total,currency,status,created_at FROM orders ORDER BY id',['reference','name','email','phone','address','city','region','postal_code','country','total_minor_units','currency','status','created_at'])}
    if kind not in queries: raise HTTPException(404,'Export not available.')
    query,headers=queries[kind]
    with database() as con: items=con.execute(query).fetchall()
    out=io.StringIO();writer=csv.writer(out);writer.writerow(headers)
    def safe(value):
        text=str(value or '')
        return "'"+text if text.startswith(('=','+','-','@','\t','\r','\n')) else text
    for item in items: writer.writerow([safe(v) for v in item])
    return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="jaghvi-{kind}.csv"'})

@app.get('/owner/security')
def owner_security(request:Request):
    require_owner(request)
    with database() as con: events=rows(con.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 30'))
    return render(request,'owner/security.html',events=events,active='security',title='Security | Jaghvi studio')

@app.post('/owner/security')
async def owner_security_save(request:Request):
    owner=require_owner(request);form=await checked_form(request)
    try:
        with database() as con: stored=con.execute('SELECT * FROM owners WHERE id=?',(owner['id'],)).fetchone()
        if not password_valid(str(form.get('current_password','')),stored['password_hash']): raise ValueError('Your current password is incorrect.')
        password=str(form.get('new_password',''));validate_password(password)
        if password!=str(form.get('confirm_password','')): raise ValueError('The new passwords do not match.')
        with database(True) as con:
            con.execute('UPDATE owners SET password_hash=? WHERE id=?',(password_hasher.hash(password),owner['id']))
            con.execute('DELETE FROM sessions WHERE owner_id=?',(owner['id'],))
            audit(con,owner['id'],'Changed password and revoked all owner sessions')
        token,session=new_session(owner['id']);request.state.new_cookie=token;request.state.session=session;request.state.data={}
    except ValueError as exc: return render(request,'owner/security.html',events=[],active='security',error=str(exc))
    flash(request,'Password changed. All previous owner sessions were signed out.');return redirect('/owner/security')
