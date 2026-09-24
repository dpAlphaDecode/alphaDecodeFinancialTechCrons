"""
Example usage of the GDFL (GlobalDataFeeds) Fundamental Data API client.

Set GDFL_ACCESS_KEY in .env (or the environment) before running.
"""
from dotenv import load_dotenv

from gdfl import GDFLClient, GDFLAPIError

load_dotenv()


def main():
    client = GDFLClient()  # reads GDFL_ACCESS_KEY from the environment

    try:
        # Quarterly profit & loss statement for a company on the BSE.
        pnl = client.get_financial_results(
            exchange="BSE",
            instrument_identifier="RELIANCE",
            year=2021,
            nature_of_report="Consolidated",
            type="ProfitLoss",
            period="QD",
        )
        print("------------------->>>>>>>>>>>",pnl)
        # items = pnl.get("Value", [])
        # print("Profit & Loss line items:", len(items))
        # for item in items[:5]:
        #     print(f"  {item['Label']}: {item['Value']}")
        #
        # # Latest financial ratios (EPS, P/E, market cap, etc).
        # ratios = client.get_financial_ratios(
        #     exchange="BSE",
        #     instrument_identifier="RELIANCE",
        #     type="EOD",
        #     nature_of_report="Consolidated",
        # )
        # print("\nFinancial ratios: EPS=%s PE=%s MarketCap=%s" % (
        #     ratios.get("EPS"), ratios.get("PeRatio"), ratios.get("MarketCap"),
        # ))

        # Corporate actions announced in a date range.
        # fin_results = client.get_(
        #     exchange="BSE",
        # )
        # print("\nCorporate actions:", fin_results)

    except GDFLAPIError as exc:
        print(f"GDFL API error: {exc} (status={exc.status_code})")


if __name__ == "__main__":
    main()
