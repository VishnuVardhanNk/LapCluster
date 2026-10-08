# Sample code with deliberate bugs, used to demonstrate a LapClusters review.
import sqlite3

ADMIN_PASSWORD = "hunter2"


def find_user(connection: sqlite3.Connection, name: str):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return connection.execute(query).fetchone()


def add_tag(tag, tags=[]):
    tags.append(tag)
    return tags


def is_admin(password):
    if password == ADMIN_PASSWORD:
        return True
