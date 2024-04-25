import json


def main():
    with open("/mnt/d/access.log", "r") as file:
        for line in file:
            try:
                json_obj = json.loads(line)
                # extract 'request' element from json, then extract 'remote_ip' element from the request:
                print(json_obj["request"]["remote_ip"])
                # if remote_ip  is null continue loop
                if json_obj["request"]["remote_ip"] is None:
                    continue
                json_str = json.dumps(json_obj)
                print(f"Length of JSON string: {len(json_str)}")
            except json.JSONDecodeError as e:
                print(f"Error: Not JSON")
            except KeyError as e:
                print(f"json object error (key not found) {e}")


if __name__ == "__main__":
    main()
