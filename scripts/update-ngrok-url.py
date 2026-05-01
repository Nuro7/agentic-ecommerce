"""
update-ngrok-url.py
Run after every `docker compose up` to sync the new ngrok URL into WordPress.
Usage: python update-ngrok-url.py
"""
import pymysql, urllib.request, json, re, sys

MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 10017
MYSQL_USER = "root"
MYSQL_PASS = "root"
MYSQL_DB   = "local"
WP_OPTION  = "wooagent_settings"

def get_ngrok_url():
    try:
        with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=5) as r:
            tunnels = json.loads(r.read())["tunnels"]
        https = next((t["public_url"] for t in tunnels if t["proto"] == "https"), None)
        return https or tunnels[0]["public_url"]
    except Exception as e:
        print(f"ERROR: Cannot reach ngrok at localhost:4040 — is Docker running? ({e})")
        sys.exit(1)

def update_wordpress(ngrok_url):
    conn = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT,
                           user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB)
    cur = conn.cursor()
    cur.execute("SELECT option_value FROM wp_options WHERE option_name=%s", (WP_OPTION,))
    row = cur.fetchone()
    if not row:
        print(f"ERROR: WordPress option '{WP_OPTION}' not found. Is the WooAgent plugin installed?")
        sys.exit(1)

    val = row[0]

    # WordPress stores options as PHP-serialized strings.
    # Pattern: s:11:"backend_url";s:NN:"OLD_URL";
    match = re.search(r'"backend_url";s:(\d+):"([^"]*)"', val)
    if match:
        old_len, old_url = match.group(1), match.group(2)
        new_val = val.replace(
            f'"backend_url";s:{old_len}:"{old_url}"',
            f'"backend_url";s:{len(ngrok_url)}:"{ngrok_url}"'
        )
        print(f"Old URL: {old_url}")
    else:
        # Fallback: try JSON (shouldn't happen with WordPress but handle gracefully)
        try:
            data = json.loads(val)
            data["backend_url"] = ngrok_url
            new_val = json.dumps(data)
        except Exception:
            print(f"ERROR: Cannot parse option value: {val[:200]}")
            sys.exit(1)

    cur.execute("UPDATE wp_options SET option_value=%s WHERE option_name=%s", (new_val, WP_OPTION))
    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    print("Fetching ngrok URL...", flush=True)
    url = get_ngrok_url()
    print(f"ngrok URL: {url}", flush=True)
    print("Updating WordPress...", flush=True)
    update_wordpress(url)
    print(f"\nDone. Backend URL set to: {url}")
    print("Hard-refresh browser (Ctrl+Shift+R) to reconnect the widget.")
