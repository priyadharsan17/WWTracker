import sys
import requests
import csv
from urllib.parse import urlparse, parse_qs
from pathlib import Path

API_URL = "https://gmserver-api.aki-game2.net/gacha/record/query"
OUTPUT_FILE = "wuwa_convene_records.csv"


def parse_convene_url(url):
    if "#" in url:
        query_part = url.split("#", 1)[1]

        if "?" in query_part:
            query_part = query_part.split("?", 1)[1]
        else:
            query_part = ""
    else:
        query_part = urlparse(url).query

    params = parse_qs(query_part)

    required = [
        "player_id",
        "record_id",
        "svr_id",
        "resources_id"
    ]

    result = {}

    for key in required:
        if key not in params:
            raise ValueError(f"Missing parameter: {key}")

        result[key] = params[key][0]

    result["lang"] = params.get("lang", ["en"])[0]

    return result


def fetch_records(info, pool_type):

    payload = {
        "playerId": info["player_id"],
        "cardPoolId": info["resources_id"],
        "cardPoolType": pool_type,
        "languageCode": info["lang"],
        "recordId": info["record_id"],
        "serverId": info["svr_id"]
    }

    response = requests.post(
        API_URL,
        json=payload,
        timeout=20
    )

    response.raise_for_status()

    return response.json()


def extract_records(data):

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in ["data", "records", "record", "list"]:

        value = data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):

            for subkey in ["records", "list", "data"]:

                if isinstance(value.get(subkey), list):
                    return value[subkey]

    return []


def save_csv(records):

    if not records:
        print("No records found.")
        return

    fieldnames = set()

    for record in records:
        if isinstance(record, dict):
            fieldnames.update(record.keys())

    preferred = [
        "time",
        "name",
        "qualityLevel",
        "cardPoolType",
        "resourceType",
        "resourceId",
        "count"
    ]

    fields = []

    for field in preferred:
        if field in fieldnames:
            fields.append(field)

    for field in sorted(fieldnames):
        if field not in fields:
            fields.append(field)

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore"
        )

        writer.writeheader()

        for record in records:
            writer.writerow(record)

    print(f"\nSaved {len(records)} records.")
    print(f"CSV: {Path(OUTPUT_FILE).resolve()}")


def main():

    if len(sys.argv) != 2:
        print("Usage:")
        print("  python wuwa_tracker.py \"<CONVENE_URL>\"")
        sys.exit(1)

    url = sys.argv[1]

    try:
        info = parse_convene_url(url)

    except Exception as e:
        print(f"Invalid URL: {e}")
        sys.exit(1)

    print("WuWa Convene Tracker")
    print("---------------------")
    print(f"Player ID : {info['player_id']}")
    print(f"Server ID : {info['svr_id']}")

    all_records = []

    # Fetch all known pool types
    for pool_type in range(1, 9):

        print(f"Fetching pool type {pool_type}...")

        try:

            data = fetch_records(
                info,
                pool_type
            )

            records = extract_records(data)

            print(f"  {len(records)} records")

            for record in records:

                if isinstance(record, dict):

                    if "cardPoolType" not in record:
                        record["cardPoolType"] = pool_type

                    all_records.append(record)

        except requests.RequestException as e:
            print(f"  Request failed: {e}")

        except Exception as e:
            print(f"  Error: {e}")

    # Remove duplicates
    unique = {}

    for record in all_records:

        key = (
            record.get("time"),
            record.get("name"),
            record.get("qualityLevel"),
            record.get("cardPoolType"),
            record.get("resourceId")
        )

        unique[key] = record

    all_records = list(unique.values())

    # Sort chronologically
    all_records.sort(
        key=lambda x: x.get("time", "")
    )

    save_csv(all_records)


if __name__ == "__main__":
    main()