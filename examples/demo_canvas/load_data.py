import fused


@fused.udf
def main():
    import pandas as pd

    return pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
