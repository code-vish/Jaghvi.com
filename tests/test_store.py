"""Integration tests use a disposable database, never the owner's real data."""
import io,json,os,re,secrets,tempfile
from pathlib import Path
import pytest
from PIL import Image
from fastapi.testclient import TestClient

_TEMP=tempfile.TemporaryDirectory(prefix='jaghvi-tests-')
os.environ['JAGHVI_DATA_DIR']=_TEMP.name
os.environ['APP_ENV']='development'
from app.main import app
from app.db import database,initialize,DB_PATH,UPLOADS,site_settings
from app.security import password_hasher,now,digest

TEST_PASSWORD='Only-for-isolated-tests-93!'

def csrf(client,path='/'):
    response=client.get(path)
    match=re.search(r'name="csrf" value="([^"]+)"',response.text)
    assert match, f'No CSRF field at {path}'
    return match.group(1)

@pytest.fixture
def client():
    for suffix in ('','-wal','-shm'):Path(str(DB_PATH)+suffix).unlink(missing_ok=True)
    for f in UPLOADS.glob('*'):f.unlink()
    initialize()
    with database(True) as con:
        con.execute('INSERT INTO owners(username,email,password_hash,created_at) VALUES (?,?,?,?)',
                    ('test-owner','owner@example.test',password_hasher.hash(TEST_PASSWORD),now()))
    with TestClient(app,follow_redirects=False) as client:yield client

def login(c):
    token=csrf(c,'/owner/login')
    r=c.post('/owner/login',data={'csrf':token,'username':'test-owner','password':TEST_PASSWORD})
    assert r.status_code==303
    return csrf(c,'/owner')

def picture(format='PNG'):
    out=io.BytesIO();Image.new('RGB',(140,180),(170,170,170)).save(out,format);return out.getvalue()

def create_product(c,status='draft',name='Test piece',photo=False,stock=5,price='1250.00'):
    token=csrf(c,'/owner/products/new')
    data={'csrf':token,'name':name,'material':'Test composition','description':'Test fixture, not a real product.',
          'price':price,'stock':str(stock),'status':status,'category':'Rings','collection_id':'1'}
    files={'photos':('test.png',picture(),'image/png')} if photo else None
    r=c.post('/owner/products/new',data=data,files=files)
    with database() as con: row=con.execute('SELECT id FROM products WHERE name=?',(name,)).fetchone()
    return r,row['id'] if row else None

def enable_orders(c,newsletter=False):
    token=csrf(c,'/owner/storefront')
    with database() as con:settings=site_settings(con)
    settings.update({'csrf':token,'contact_email':'shop@example.test','order_requests_enabled':True,
                     'newsletter_enabled':newsletter,'privacy_policy':'Test privacy policy.',
                     'terms_policy':'Test terms.','shipping_policy':'Test shipping.','returns_policy':'Test returns.'})
    data={k:('on' if v is True else str(v)) for k,v in settings.items() if v is not False}
    return c.post('/owner/storefront',data=data)

@pytest.mark.parametrize('url',['/','/shop','/story','/contact','/bag','/wishlist','/policies/privacy','/owner/login','/robots.txt','/sitemap.xml'])
def test_empty_public_pages(client,url):
    r=client.get(url);assert r.status_code==200
    assert 'Test piece' not in r.text

def test_all_owner_pages_require_login(client):
    for path in ['/owner','/owner/products','/owner/products/new','/owner/collections','/owner/storefront','/owner/orders','/owner/inbox','/owner/subscribers','/owner/security','/owner/export/orders']:
        r=client.get(path);assert r.status_code==303;assert r.headers['location']=='/owner/login'

def test_owner_pages_render(client):
    login(client)
    for path in ['/owner','/owner/products','/owner/products/new','/owner/collections','/owner/storefront','/owner/orders','/owner/inbox','/owner/subscribers','/owner/security']:
        r=client.get(path);assert r.status_code==200,(path,r.text)

def test_session_cookie_and_security_headers(client):
    r=client.get('/')
    assert 'HttpOnly' in r.headers['set-cookie'];assert 'SameSite=lax' in r.headers['set-cookie']
    assert r.headers['x-frame-options']=='DENY';assert r.headers['x-content-type-options']=='nosniff'
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']

def test_csrf_required_and_wrong_rejected(client):
    login(client)
    for data in [{'name':'Injected piece'},{'csrf':'wrong','name':'Injected piece'}]:
        assert client.post('/owner/products/new',data=data).status_code==403
    with database() as con:assert con.execute('SELECT COUNT(*) FROM products').fetchone()[0]==0

def test_anonymous_cannot_upload(client):
    token=csrf(client,'/owner/login')
    r=client.post('/owner/products/new',data={'csrf':token,'name':'unauthorized'},files={'photos':('test.png',picture(),'image/png')})
    assert r.status_code==303
    with database() as con:assert con.execute('SELECT COUNT(*) FROM products').fetchone()[0]==0

def test_login_rotates_session_and_logout_revokes(client):
    csrf(client,'/owner/login');old=client.cookies.get('jaghvi_session')
    token=login(client);new=client.cookies.get('jaghvi_session');assert old!=new
    with database() as con:assert not con.execute('SELECT 1 FROM sessions WHERE token_hash=?',(digest(old),)).fetchone()
    assert client.post('/owner/logout',data={'csrf':token}).status_code==303
    assert client.get('/owner').status_code==303
    with database() as con:assert not con.execute('SELECT 1 FROM sessions WHERE token_hash=?',(digest(new),)).fetchone()

def test_draft_without_photos_persists_but_is_hidden(client):
    login(client);r,pid=create_product(client,price='')
    assert r.status_code==303;assert pid
    with database() as con:assert con.execute('SELECT price FROM products WHERE id=?',(pid,)).fetchone()['price']==0
    assert 'Test piece' in client.get('/owner/products').text
    assert 'Test piece' not in client.get('/shop').text
    assert client.get('/pieces/test-piece').status_code==404

def test_publishing_requires_real_photo(client):
    login(client);r,pid=create_product(client,status='published')
    assert r.status_code==200 and 'at least one real product photograph' in r.text
    assert pid is None

def test_real_upload_publication_and_image_reencode(client):
    login(client);r,pid=create_product(client,status='published',photo=True)
    assert r.status_code==303
    assert 'Test piece' in client.get('/shop').text
    with database() as con: image=con.execute('SELECT * FROM images WHERE product_id=?',(pid,)).fetchone()
    assert image['path'].endswith('.webp')
    assert client.get(image['path']).status_code==200
    assert client.get('/pieces/test-piece').status_code==200
    with Image.open(UPLOADS/image['path'].split('/')[-1]) as im:assert im.format=='WEBP';assert not im.getexif()

def test_invalid_upload_rejected(client):
    login(client);token=csrf(client,'/owner/products/new')
    data={'csrf':token,'name':'Malicious','price':'100','stock':'2','status':'draft','category':'Rings'}
    for filename,body,mime in [('bad.svg',b'<svg onload="alert(1)"></svg>','image/svg+xml'),('bad.png',b'not an image','image/png')]:
        r=client.post('/owner/products/new',data=data,files={'photos':(filename,body,mime)})
        assert r.status_code==200
    with database() as con:assert con.execute('SELECT COUNT(*) FROM products').fetchone()[0]==0
    assert not list(UPLOADS.iterdir())

def test_name_html_escaped_and_sql_search_safe(client):
    login(client);r,pid=create_product(client,name='<script>alert(1)</script>',photo=True,status='published')
    assert r.status_code==303
    page=client.get('/shop').text
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page
    assert client.get('/shop',params={'q':"' OR 1=1 --"}).status_code==200

def test_settings_persist_and_require_policies(client):
    login(client);token=csrf(client,'/owner/storefront')
    r=client.post('/owner/storefront',data={'csrf':token,'hero_title':'A new beginning','order_requests_enabled':'on'})
    assert r.status_code==303
    with database() as con:assert not site_settings(con)['order_requests_enabled'];assert site_settings(con)['hero_title']!='A new beginning'
    client.post('/owner/storefront',data={'csrf':token,'hero_title':'A new beginning'})
    assert 'A new beginning' in client.get('/').text

def test_archive_hides_product(client):
    login(client);_,pid=create_product(client,status='published',photo=True)
    token=csrf(client,f'/owner/products/{pid}')
    r=client.post(f'/owner/products/{pid}/archive',data={'csrf':token});assert r.status_code==303
    assert 'Test piece' not in client.get('/shop').text
    assert client.get('/pieces/test-piece').status_code==404

def test_removing_last_image_unpublishes(client):
    login(client);_,pid=create_product(client,status='published',photo=True)
    with database() as con:image=con.execute('SELECT * FROM images WHERE product_id=?',(pid,)).fetchone()
    r=client.post(f'/owner/products/{pid}/images/{image["id"]}/remove',data={'csrf':csrf(client,f'/owner/products/{pid}')})
    assert r.status_code==303
    with database() as con:assert con.execute('SELECT status FROM products WHERE id=?',(pid,)).fetchone()[0]=='draft'
    assert not (UPLOADS/image['path'].split('/')[-1]).exists()

def test_wrong_variant_rejected_and_server_prices(client):
    login(client);_,pid=create_product(client,status='published',photo=True)
    token=csrf(client,f'/owner/products/{pid}')
    client.post(f'/owner/products/{pid}/variants',data={'csrf':token,'label':'Size 7','price':'1500','stock':'3'})
    with database() as con:vid=con.execute('SELECT id FROM variants WHERE product_id=?',(pid,)).fetchone()[0]
    token=csrf(client,'/pieces/test-piece')
    assert client.post('/bag/add',data={'csrf':token,'product_id':pid,'variant_id':999,'quantity':1}).status_code==400
    assert client.post('/bag/add',data={'csrf':token,'product_id':pid,'variant_id':vid,'quantity':1,'price':'1'}).status_code==303
    assert '1,500' in client.get('/bag').text

def test_complete_order_request_stock_lifecycle_and_idempotency(client):
    login(client);_,pid=create_product(client,status='published',photo=True,stock=5)
    assert enable_orders(client).status_code==303
    token=csrf(client,'/pieces/test-piece')
    assert client.post('/bag/add',data={'csrf':token,'product_id':pid,'quantity':2}).status_code==303
    checkout=client.get('/checkout');assert checkout.status_code==200
    nonce=re.search(r'name="nonce" value="([^"]+)"',checkout.text).group(1)
    token=re.search(r'name="csrf" value="([^"]+)"',checkout.text).group(1)
    data={'csrf':token,'nonce':nonce,'name':'Test Customer','email':'buyer@example.test','phone':'1234567890',
          'address':'Test address','city':'Test city','region':'Test region','postal_code':'000000','country':'India','consent':'on','total':'1'}
    result=client.post('/checkout',data=data);assert result.status_code==303
    assert client.get(result.headers['location']).status_code==200
    assert client.post('/checkout',data=data).status_code==409
    with database() as con:
        order=con.execute('SELECT * FROM orders').fetchone();assert order['total']==250000
        assert con.execute('SELECT stock FROM products WHERE id=?',(pid,)).fetchone()[0]==5
    token=csrf(client,f'/owner/orders/{order["id"]}')
    client.post(f'/owner/orders/{order["id"]}',data={'csrf':token,'status':'confirmed'})
    with database() as con:assert con.execute('SELECT stock FROM products WHERE id=?',(pid,)).fetchone()[0]==3
    client.post(f'/owner/orders/{order["id"]}',data={'csrf':token,'status':'confirmed'})
    with database() as con:assert con.execute('SELECT stock FROM products WHERE id=?',(pid,)).fetchone()[0]==3
    client.post(f'/owner/orders/{order["id"]}',data={'csrf':token,'status':'cancelled'})
    with database() as con:assert con.execute('SELECT stock FROM products WHERE id=?',(pid,)).fetchone()[0]==5

def test_order_view_not_exposed_to_other_sessions(client):
    with database(True) as con:
        con.execute('''INSERT INTO orders(reference,session_hash,name,email,phone,address,city,region,postal_code,country,subtotal,shipping,total,currency,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',('JV-PRIVATE','different-session','Private Customer','private@example.test','123','address','city','region','zip','country',100,0,100,'INR',now()))
    assert client.get('/order/JV-PRIVATE').status_code==404

def test_newsletter_contact_and_csv_export(client):
    login(client);create_product(client,status='published',photo=True);enable_orders(client,newsletter=True)
    token=csrf(client,'/')
    r=client.post('/subscribe',data={'csrf':token,'email':'person@example.test','consent':'on'});assert r.status_code==303
    r=client.post('/contact',data={'csrf':token,'name':'A customer','email':'person@example.test','message':'Hello from a test.','consent':'on'});assert r.status_code==303
    assert 'person@example.test' in client.get('/owner/subscribers').text
    assert 'Hello from a test.' in client.get('/owner/inbox').text
    csv=client.get('/owner/export/subscribers');assert csv.status_code==200;assert 'person@example.test' in csv.text

def test_rate_limit_login(client):
    token=csrf(client,'/owner/login')
    for _ in range(8):
        assert client.post('/owner/login',data={'csrf':token,'username':'test-owner','password':'wrong'}).status_code==401
    assert client.post('/owner/login',data={'csrf':token,'username':'test-owner','password':'wrong'}).status_code==429

def test_body_size_limit_without_header_trust(client):
    body=b'x'*(32*1024*1024+1)
    assert client.post('/owner/login',content=body,headers={'content-type':'application/octet-stream'}).status_code==413

def test_password_change_revokes_sessions(client):
    token=login(client);old=client.cookies.get('jaghvi_session')
    token=csrf(client,'/owner/security')
    r=client.post('/owner/security',data={'csrf':token,'current_password':TEST_PASSWORD,
            'new_password':'Different-private-passphrase-42!','confirm_password':'Different-private-passphrase-42!'})
    assert r.status_code==303;assert old!=client.cookies.get('jaghvi_session')
    with database() as con:assert not con.execute('SELECT 1 FROM sessions WHERE token_hash=?',(digest(old),)).fetchone()
