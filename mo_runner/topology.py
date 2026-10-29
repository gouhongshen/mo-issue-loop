"""Two CNs with independently faultable CN1, one TN and one LOG."""
import json
from pathlib import Path
import uuid


SQL_PORTS = (26001, 26002)


def configure(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    log_id = str(uuid.uuid4())
    for index, role in enumerate(("LOG", "TN", "CN", "CN")):
        name = "cn1" if index == 3 else role.lower()
        text = ('service-type = "' + role + '"\ndata-dir = "./data"\n'
                '[log]\nlevel = "warn"\nmax-size = 64\n'
                '[observability]\ndisableTrace = true\ndisableMetric = true\n'
                'status-port = ' + str(27080 + index) + '\n'
                '[hakeeper-client]\nservice-addresses = ["127.0.0.1:27001"]\n')
        if role == "LOG":
            text += ('[logservice]\nuuid = "' + log_id + '"\nservice-host = "127.0.0.1"\n'
                     'raft-port = 27000\nlogservice-port = 27001\ngossip-port = 27002\n'
                     'gossip-seed-addresses = ["127.0.0.1:27002"]\n'
                     '[logservice.BootstrapConfig]\nbootstrap-cluster = true\n'
                     'num-of-log-shards = 1\nnum-of-tn-shards = 1\nnum-of-log-shard-replicas = 1\n'
                     'init-hakeeper-members = ["131072:' + log_id + '"]\n')
        else:
            text += ('[[fileservice]]\nname = "LOCAL"\nbackend = "DISK"\ndata-dir = "./data/local-' + name + '"\n'
                     '[[fileservice]]\nname = "SHARED"\nbackend = "DISK"\ndata-dir = "./data/shared"\n'
                     '[fileservice.cache]\nmemory-capacity = "128MB"\ndisk-capacity = "512MB"\n'
                     'disk-path = "./data/cache-' + name + '"\n'
                     '[[fileservice]]\nname = "ETL"\nbackend = "DISK-ETL"\n'
                     '[' + role.lower() + ']\nuuid = "' + str(uuid.uuid4()) + '"\n'
                     'service-host = "127.0.0.1"\nport-base = ' + str(27010 if role == "TN" else 27050 if index == 3 else 27030) + '\n')
            if role == "CN":
                text += '[cn.frontend]\nhost = "0.0.0.0"\nport = ' + str(SQL_PORTS[index == 3]) + '\n'
        (root / (name + ".toml")).write_text(text)
    (root / "launch.toml").write_text('\n'.join(key + ' = ' + json.dumps(value) for key, value in {
        "logservices": ["./log.toml"], "tnservices": ["./tn.toml"], "cnservices": ["./cn.toml"]}.items()))
