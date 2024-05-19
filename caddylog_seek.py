import json

import time
import json


def tail_f(file):
    file.seek(0, 2)
    while True:
        line = file.readline()
        if not line:
            time.sleep(0.1)
            continue
        yield line


def main():
    data = {}  # New data structure
    with open("/mnt/d/access.log", "r") as file:
        for line in tail_f(file):
            try:
                json_obj = json.loads(line)
                remoteIp = json_obj["request"]["remote_ip"]
                uri = json_obj["request"]["uri"]
                status = json_obj["status"]
                if remoteIp is None or remoteIp.startswith("192.168"):
                    continue
                add_data(data, remoteIp, uri, status)
            except (json.JSONDecodeError, KeyError):
                pass


if __name__ == "__main__":
    main()
