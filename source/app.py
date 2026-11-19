"""运输交接台与可连接邮箱，共享外部演练阶段。"""
from .common import *
from .mailbox import create_mailbox
class App(Application):
    def __init__(self): super().__init__();self.mail_port=None
    def handle(self,h):
        p=h.path_only
        if p=='/': return self.document(h,'运输邮件与入场交接',view({'imap_host':'127.0.0.1','imap_port':self.mail_port,'mailboxes':['INBOX','Archive']}))
        if p=='/mail-info': return self.emit(h,{'port':self.mail_port,'mailboxes':['INBOX','Archive']})
        if p in ['/handoff','/stop']:
            bucket='handoffs' if p=='/handoff' else 'stops'
            if h.command=='POST':
                item=self.record(bucket,h.body['reference'],h.body)
                if bucket=='handoffs': item['boxes']=[{'box':b,'state':'accepted'} for b in h.body['boxes']]
                if h.body.get('lose_response'): return h.send(b'','text/plain',503)
            for item in self.remote.get('stops',{}).values(): item['state']='confirmed' if self.data('stop_confirm') else 'pending'
            return self.emit(h,self.remote.get(bucket,{}))
        return self.emit(h,{'error':'not_found'},404)
def start(port=8765,mail_port=1143):
    app=App();mail=create_mailbox(app,mail_port);app.mail_port=mail.server_address[1];threading.Thread(target=mail.serve_forever,daemon=True).start();return serve(app,port),mail
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--mail-port',type=int,default=1143);args=parser.parse_args();http,mail=start(args.port,args.mail_port)
    print('本地邮件与交接台已启动',flush=True)
    try: http.serve_forever()
    finally: http.server_close();mail.shutdown();mail.server_close()
