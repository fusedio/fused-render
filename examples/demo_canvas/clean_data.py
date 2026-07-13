import fused


@fused.udf
def main(threshold: int = 0):
    import pandas as pd

    df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
    return df[df["value"] > threshold]
