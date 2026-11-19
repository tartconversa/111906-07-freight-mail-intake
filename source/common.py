"""本地合成来源服务的传输支撑，不执行跨站业务。"""
import json,html,secrets,threading,argparse,hashlib,io,zipfile
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from email.parser import BytesParser
from email.policy import default
ROOT=Path(__file__).resolve().parents[1]
def digest(data): return hashlib.sha256(data).hexdigest()
def packed(files):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        for name,data in files.items(): z.writestr(name,data)
    return stream.getvalue()
def dump(value): return json.dumps(value,ensure_ascii=False,sort_keys=True)
def page(title,body):
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><title>'+html.escape(title)+'</title><style>body{font:18px sans-serif;max-width:1050px;margin:30px auto;line-height:1.6}td,th{border:1px solid #aaa;padding:8px}pre{white-space:pre-wrap}label{display:block}button,input,textarea,select{font:inherit;margin:6px}textarea{width:90%;height:120px}</style><h1>'+html.escape(title)+'</h1>'+body+'</html>').encode()
def view(value): return '<pre>'+html.escape(dump(value))+'</pre>'
def form(action,fields,button='提交'):
    return '<form method="post" enctype="multipart/form-data" action="'+action+'">'+''.join('<label>'+label+'<input name="'+name+'" type="'+kind+'" value="'+html.escape(value)+'"></label>' for name,label,kind,value in fields)+'<button>'+button+'</button></form>'
class Application:
    def __init__(self):
        self.seed=json.loads((ROOT/'fixtures/source.json').read_text(encoding='utf-8'))
        self.stage=0;self.remote={};self.lock=threading.RLock();self.sessions=set();self.events=[]
    @property
    def current(self): return self.seed['stages'][self.stage]
    def data(self,key): return self.current[key]
    def emit(self,h,value,status=200,headers=None):
        return h.send(dump(value).encode(),'application/json; charset=utf-8',status,headers)
    def document(self,h,title,body): return h.send(page(title,body),'text/html; charset=utf-8')
    def record(self,collection,reference,value):
        bucket=self.remote.setdefault(collection,{})
        if reference in bucket:
            return bucket[reference]
        item={'receipt':collection+'-'+str(len(bucket)+1),'reference':reference,'payload':value,'state':'received'}
        bucket[reference]=item;return item
    def control(self,h):
        if h.headers.get('X-Operator')!='local-operator': return self.emit(h,{'error':'operator_required'},403)
        if h.path_only=='/__control':
            self.stage=int(h.body.get('stage',0))
            if not 0<=self.stage<len(self.seed['stages']): self.stage=0;return self.emit(h,{'error':'stage'},400)
            return self.emit(h,{'stage':self.stage})
        return self.emit(h,{'stage':self.stage,'remote':self.remote,'events':self.events})
    def handle(self,h): raise NotImplementedError
def serve(app,port=8765):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def send(self,data,kind,status=200,headers=None):
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            if getattr(self,'new_cookie',None): self.send_header('Set-Cookie','session='+self.new_cookie+'; HttpOnly; SameSite=Lax; Path=/')
            for key,value in (headers or {}).items(): self.send_header(key,str(value))
            self.end_headers()
            try: self.wfile.write(data);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError): pass
        def do_GET(self): self.dispatch()
        def do_POST(self): self.dispatch()
        def dispatch(self):
            self.new_cookie=None
            parsed=urlparse(self.path);self.path_only=parsed.path;self.query={k:v[-1] for k,v in parse_qs(parsed.query).items()};self.body={};self.files={}
            raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
            kind=self.headers.get('Content-Type','')
            if raw and 'application/json' in kind: self.body=json.loads(raw)
            elif raw and 'multipart/form-data' in kind:
                msg=BytesParser(policy=default).parsebytes(('Content-Type: '+kind+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                for part in msg.iter_parts():
                    name=part.get_param('name',header='content-disposition');payload=part.get_payload(decode=True)
                    if part.get_filename(): self.files[name]={'filename':part.get_filename(),'bytes':payload}
                    else: self.body[name]=payload.decode('utf-8')
            elif raw: self.body={k:v[-1] for k,v in parse_qs(raw.decode()).items()}
            cookie=self.headers.get('Cookie','');self.session=next((s for s in app.sessions if 'session='+s in cookie),None)
            if not self.session:
                self.new_cookie=secrets.token_hex(12);self.session=self.new_cookie;app.sessions.add(self.session)
            with app.lock:
                if self.path_only.startswith('/__'): return app.control(self)
                app.events.append({'method':self.command,'path':self.path_only,'stage':app.stage})
                if self.path_only=='/health': return app.emit(self,{'status':'ok','stage':app.stage})
                try: return app.handle(self)
                except (KeyError,ValueError) as exc: return app.emit(self,{'error':'invalid_request','detail':str(exc)},400)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    return server
def main(app):
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    server=serve(app,args.port)
    print('本地来源已启动 http://127.0.0.1:'+str(server.server_port),flush=True)
    try: server.serve_forever()
    finally: server.server_close()
