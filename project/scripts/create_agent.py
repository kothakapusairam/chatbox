"""
Create an agent or administrator from the command line (use this to bootstrap production).

  python scripts/create_agent.py --username jdoe --name "Jane Doe" --admin
The password is prompted for (never passed as an argument, so it stays out of shell history).
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402
from config import config_by_name  # noqa: E402
from security import validate_password, valid_username  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email")
    parser.add_argument("--admin", action="store_true", help="grant administrator access")
    args = parser.parse_args()

    cfg = config_by_name[os.environ.get("FLASK_ENV", "development")]
    username = args.username.strip().lower()
    if not valid_username(username):
        sys.exit("Username must be 3-32 characters: lowercase letters, digits, . _ -")

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        sys.exit("Passwords do not match.")
    problems = validate_password(password, username, cfg.PASSWORD_MIN_LENGTH)
    if problems:
        sys.exit(" ".join(problems))

    conn = database.init_db(cfg.DATABASE_PATH, seed_data=False)
    try:
        if database.get_agent_by_username(conn, username):
            sys.exit(f"User '{username}' already exists.")
        database.create_agent(conn, username, generate_password_hash(password), args.name,
                              access_level="admin" if args.admin else "agent",
                              role="Administrator" if args.admin else "Support Specialist",
                              must_change_password=True, email=args.email)
        database.add_audit(conn, "cli", "user.created", "agent", username, {"access_level": "admin" if args.admin else "agent"})
        conn.commit()
    finally:
        conn.close()
    print(f"Created {'administrator' if args.admin else 'agent'} '{username}'. They must change the password at first sign-in.")


if __name__ == "__main__":
    main()
