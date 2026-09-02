"""Schema for the 1934-1942 Rand McNally directory.

Changes vs. the base schema:
- Balance sheet values are reported "In Thousands of Dollars"
- Statement date added (e.g., "Dec'33Stmt" -> "1933-12")
- Liabilities: "Surplus and Profits" split into "Surplus" + "Undivided Profits and/or Reserves"
- Liabilities: "Totals" column added
- Assets: "Bonds and Securities" split into "U.S. Gov. Securities" + "Other Securities"
- Assets: "Miscellaneous Assets" renamed to "Other Resources"
- Column order changed (see prompt_1934.md for details)
"""

from pydantic import BaseModel, Field

from schema_1911 import Correspondent


class Bank(BaseModel):
    state_abbreviation: str | None = Field(None, description="Two-digit state abbreviation detected from table header (e.g., RI)")

    # Location Info (Column 1)
    town_name: str | None = Field(None, description="Name of the town")
    town_pop: int | None = Field(None, description="Population of the town")
    county_name: str | None = Field(None, description="County name")

    # Bank Identity (Column 2)
    bank_name: str = Field(..., description="Legal name of the bank")
    old_name: str | None = Field(None, description="Former name of the bank, e.g. 'Valley Bank' from '(Formerly Valley Bank)'")
    statement_date: str | None = Field(None, description="Date of the balance sheet statement in YYYY-MM format (e.g., '1933-12' from 'Dec\\'33Stmt')")
    established_year: int | None = Field(None, description="Year established (YYYY or YY)")
    transit_number: str | None = Field(None, description="ABA Transit number (e.g. 57-123)")

    # Boolean Flags (Symbols)
    ch_member: int = Field(0, description="1 for Member (★), 2 for Affiliate (+), None otherwise")
    is_state: bool = Field(False, description="True if § symbol is present")
    is_private: bool = Field(False, description="True if † symbol is present")
    is_aba: bool = Field(False, description="True if ● symbol is present")
    is_sba: bool = Field(False, description="True if ‡ symbol is present")
    is_fed: bool = Field(False, description="True if ◆ symbol is present")

    # Officers (Columns 3-6)
    president: str | None = Field(None, description="Name of the President")
    vice_president: str | None = Field(None, description="Name of the Vice President")
    cashier: str | None = Field(None, description="Name of the Cashier")
    assistant_cashier: str | None = Field(None, description="Name of the Assistant Cashier")

    # Bank Events (from Column 3, for closed/liquidated/merged banks)
    event: str | None = Field(None, description="Event such as 'Liquidation', 'Merger with Bank XYZ', 'Closed for liquidation'")
    event_date: str | None = Field(None, description="Date of the event in YYYY-MM-DD format")

    # Liabilities (Columns 7-12)
    is_branch: bool = Field(False, description="True if this is a branch of another bank")
    capital: float | None = Field(None, description="Capital (common stock)")
    capital_preferred: float | None = Field(None, description="Preferred stock capital (from 'Pf. XX' annotation below the capital figure)")
    surplus: float | None = Field(None, description="Surplus")
    undivided_profits: float | None = Field(None, description="Undivided Profits and/or Reserves")
    surplus_consolidated: bool = Field(False, description="True if undivided profits cell says '(With Surp.)' indicating surplus and undivided profits are combined in the surplus column")
    includes_reserves: bool = Field(False, description="True if undivided profits cell says '(Incl. Res.)' indicating the value includes reserves")
    deposits: float | None = Field(None, description="Deposits")
    other_liabs: float | None = Field(None, description="Other Liabilities")
    totals: float | None = Field(None, description="Totals (sum of all liabilities)")

    # Resources (Columns 13-17)
    cash: float | None = Field(None, description="Cash, Exchanges & Due from Banks")
    us_gov_securities: float | None = Field(None, description="U.S. Government Securities")
    other_securities: float | None = Field(None, description="Other Securities")
    loans: float | None = Field(None, description="Loans and Discounts")
    other_resources: float | None = Field(None, description="Other Resources")

    # Correspondents (Column 18)
    correspondents: list[Correspondent] = Field(default_factory=list, description="List of principal correspondent banks")
    correspondents_raw: str | None = Field(None, description="Raw string underlying the list of principal correspondent banks")

    # Miscellaneous
    misc_notes: str | None = Field(None, description="Additional row text including branch notes, advertising, or collection details.")


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")
