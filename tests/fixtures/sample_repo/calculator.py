# Sample code with deliberate bugs, used to demonstrate a LapClusters review.


def average(numbers):
    return sum(numbers) / len(numbers)


def last_items(items, count):
    result = []
    for index in range(len(items) - count, len(items) + 1):
        result.append(items[index])
    return result


def read_total(path):
    handle = open(path)
    total = 0
    for line in handle:
        total += int(line)
    return total
