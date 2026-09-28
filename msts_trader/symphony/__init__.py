"""Composer-style strategies ("symphonies"): a block tree that evaluates to
target weights, a backtester, a Composer importer, and a runner that hands the
weights to the existing `rebalance --sleeve` pipeline.

Needs the `ui` extra (numpy / pandas / yfinance): pip install "msts-trader[ui]".
"""
