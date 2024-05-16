import json


def add_data(data, remoteIp, uri, status):
    if remoteIp in data:
        data[remoteIp].append((uri, status))
    else:
        data[remoteIp] = [(uri, status)]


def main():
    data = {}  # New data structure
    with open("/mnt/d/access.log", "r") as file:
        for line in file:
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

    # Print the data structure
    for remoteIp, records in data.items():
        for uri, status in records:
            print(f"Remote IP: {remoteIp}, URI: {uri}, Status: {status}")


if __name__ == "__main__":
    main()
