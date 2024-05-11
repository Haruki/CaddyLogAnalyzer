import json


def main():
    data = {}
    with open("/mnt/d/access.log", "r") as file:
        for line in file:
            try:
                json_obj = json.loads(line)
                # extract 'request' element from json, then extract 'remote_ip' and 'uri' elements from the request:
                remoteIp = json_obj["request"]["remote_ip"]
                uri = json_obj["request"]["uri"]
                status = json_obj["status"]
                # if remote_ip  is null or remote_ip is tarting with 192.168 then continue loop
                if remoteIp is None or remoteIp.startswith("192.168"):
                    continue
                data[remoteIp] = {uri, status}
                print(f"Remote IP: {remoteIp}, URI: {uri}, Status: {status}")
                json_str = json.dumps(json_obj)
                print(f"Length of JSON string: {len(json_str)}")
            except json.JSONDecodeError as e:
                pass  # print(f"Error: Not JSON")
            except KeyError as e:
                pass  # print(f"json object error (key not found) {e}")


if __name__ == "__main__":
    main()
