import csv


def export(rows, path):
    # Loads every row into memory first: fine for small tables,
    # a MemoryError waiting to happen for big ones.
    data = list(rows)
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(data)
