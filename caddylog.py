import json


def main():
    with open("/mnt/d/access.log", "r") as file:
        for line in file:
            try:
                json_obj = json.loads(line)
                json_str = json.dumps(json_obj)
                print(f"Length of JSON string: {len(json_str)}")
            except json.JSONDecodeError as e:
                print(f"Error: Not JSON")


if __name__ == "__main__":
    main()
