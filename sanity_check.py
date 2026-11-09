import sqlite3
import os

# ========== CONFIGURATION ==========
DB_NAME = "anime.db"
TABLE_NAME = "anime"
COLUMN_NAME = "mal_id"
FOLDER_PATH = "covers"
# ===================================


def check_missing_files():
    choice = input(
        "1. Number of missing files (total count)\n"
        "2. Individual missing IDs\n"
        "Choice: "
    ).strip()

    if choice not in ("1", "2"):
        print("Invalid choice.")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(f'SELECT "{COLUMN_NAME}" FROM "{TABLE_NAME}"')
    values = cursor.fetchall()
    conn.close()

    numbers = {
        int(row[0]) for row in values
        if row[0] is not None and str(row[0]).isdigit()
    }

    # Ensure the folder exists to avoid FileNotFoundError
    if not os.path.exists(FOLDER_PATH):
        print(f"Error: Folder '{FOLDER_PATH}' does not exist.")
        return

    existing_files = {
        os.path.splitext(filename)[0]
        for filename in os.listdir(FOLDER_PATH)
        if os.path.isfile(os.path.join(FOLDER_PATH, filename))
    }

    missing = sorted(
        number for number in numbers
        if str(number) not in existing_files
    )

    # Modified output logic based on your request
    if choice == "1":
        print(f"Total missing files: {len(missing)}")
    else:
        print(f"Total missing files: {len(missing)}")
        for number in missing:
            print(f"ID: {number}")


if __name__ == "__main__":
    check_missing_files()
