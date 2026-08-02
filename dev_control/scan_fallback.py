"""nmap 不存在时使用的无特权 TCP connect 扫描器。"""
import ipaddress, socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.sax.saxutils import escape

COMMON=[20,21,22,23,25,53,67,68,69,80,110,111,123,135,137,138,139,143,161,389,443,445,465,514,587,631,636,873,993,995,1080,1433,1521,1883,2049,2375,3000,3306,3389,5000,5432,5672,5900,6379,6443,6999,7000,7001,7500,8000,8001,8080,8081,8088,8443,8888,9000,9090,9200,9999,10000,27017]

def ports(top_ports=1000,custom=""):
    out=[]
    if custom:
        for part in str(custom).split(","):
            part=part.strip()
            try:
                if "-" in part:
                    a,b=map(int,part.split("-",1));out.extend(range(max(1,a),min(65535,b)+1))
                else:out.append(int(part))
            except ValueError:continue
    else:
        count=max(1,min(int(top_ports),2000));out=COMMON[:]
        out.extend(p for p in range(1,count+1) if p not in out)
    return sorted({p for p in out if 1<=p<=65535})

def scan(ip,top_ports=1000,custom_ports=""):
    try:ipaddress.ip_address(ip)
    except ValueError:
        try:ip=socket.gethostbyname(ip)
        except OSError as e:return "",f"主机地址无效：{e}"
    targets=ports(top_ports,custom_ports)
    if not targets:return "","没有有效的扫描端口"
    def one(port):
        sock=socket.socket();sock.settimeout(.45)
        try:return port,sock.connect_ex((ip,port))==0
        except OSError:return port,False
        finally:sock.close()
    opened=[]
    with ThreadPoolExecutor(max_workers=min(200,len(targets))) as pool:
        futures=[pool.submit(one,p) for p in targets]
        for future in as_completed(futures):
            p,ok=future.result()
            if ok:opened.append(p)
    nodes=[]
    for p in sorted(opened):
        try:service=socket.getservbyport(p,"tcp")
        except OSError:service="unknown"
        nodes.append(f'<port protocol="tcp" portid="{p}"><state state="open"/><service name="{escape(service)}"/></port>')
    xml='<?xml version="1.0"?><nmaprun><host><status state="up"/><address addr="'+escape(ip)+'" addrtype="ipv4"/><ports>'+"".join(nodes)+"</ports></host></nmaprun>"
    return xml,""
