"""Buggy Python script — NameError: undefined variable."""


def calculate_ratio(numerator, denominator):
    if denominator == 0:
        return 0
    return numerator / denom  # BUG: typo — should be 'denominator'


def main():
    result = calculate_ratio(10, 2)
    print(f"Result: {result}")


if __name__ == "__main__":
    main()
