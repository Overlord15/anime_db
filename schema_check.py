import os
import sqlite3
import sys


def print_database_schema(db_path):
    if not os.path.exists(db_path):
        print(f"Error: The file '{db_path}' does not exist.")
        return

    try:
        # Connect to the SQLite database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Query the sqlite_master table to get table names and their SQL creation schema
        cursor.execute(
            """
            SELECT name, sql 
            FROM sqlite_master 
            WHERE type='table' AND name NOT LIKE 'sqlite_%';
        """
        )
        tables = cursor.fetchall()

        if not tables:
            print(
                f"No user-defined tables found in the database '{db_path}'."
            )
            return

        print(f"=== SCHEMA FOR DATABASE: {db_path} ===")
        for table_name, schema_sql in tables:
            print(f"\nTable: {table_name}")
            print("-" * (len(table_name) + 7))
            if schema_sql:
                print(schema_sql.strip())
            else:
                print("(No schema found / Virtual Table)")

    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}")
    finally:
        if "conn" in locals():
            conn.close()


if __name__ == "__main__":
    # Expects the database path as a command-line argument
    if len(sys.argv) < 2:
        print("Usage: python script.py <path_to_db_file>")
        sys.exit(1)

    database_file = sys.argv[1]
    print_database_schema(database_file)
