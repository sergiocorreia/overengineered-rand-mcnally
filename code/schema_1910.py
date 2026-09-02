"""Schema for the 1910 Rand McNally directory.

Changes vs. schema_1911:
- `transit_number` is replaced by `charter_number` (the charter number is
  printed under the bank name; the ABA transit number was only adopted in 1911).
- `is_fed` is dropped (the Federal Reserve System was established in 1913; the
  `◆` symbol does not appear in 1910).

New symbols introduced in 1910 (already present in schema_1911 and kept here):
- `●` ABA member (`is_aba`)
- `‡` state bankers association member (`is_sba`)
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
    established_year: int | None = Field(None, description="Year established (YYYY or YY)")
    charter_number: str | None = Field(None, description="Charter number (printed under the bank name in 1910)")

    # Boolean Flags (Symbols)
    ch_member: int = Field(0, description="1 for Member (★), 2 for Affiliate (+), None otherwise")
    is_state: bool = Field(False, description="True if § symbol is present")
    is_private: bool = Field(False, description="True if † symbol is present")
    is_aba: bool = Field(False, description="True if ● symbol is present (Member ABA)")
    is_sba: bool = Field(False, description="True if ‡ symbol is present (Member State Banking Assoc)")

    # Officers (Columns 3-6)
    president: str | None = Field(None, description="Name of the President")
    vice_president: str | None = Field(None, description="Name of the Vice President")
    cashier: str | None = Field(None, description="Name of the Cashier")
    assistant_cashier: str | None = Field(None, description="Name of the Assistant Cashier")

    # Bank Events
    event: str | None = Field(None, description="Event such as 'Liquidation', 'Merger with Bank XYZ', 'Closed for liquidation'")
    event_date: str | None = Field(None, description="Date of the event in YYYY-MM-DD format")

    # Liabilities (Columns 7-10)
    is_branch: bool = Field(False, description="True if this is a branch of another bank")
    capital: float | None = Field(None, description="Paid-Up Capital")
    surplus: float | None = Field(None, description="Surplus and Profits")
    other_liabs: float | None = Field(None, description="Other Liabilities")
    deposits: float | None = Field(None, description="Deposits")

    # Assets (Columns 11-14)
    loans: float | None = Field(None, description="Loans and Discounts")
    bonds: float | None = Field(None, description="Bonds and Securities")
    other_assets: float | None = Field(None, description="Miscellaneous Assets")
    cash: float | None = Field(None, description="Cash and Exchanges")

    # Correspondents (Column 15)
    correspondents: list[Correspondent] = Field(default_factory=list, description="List of principal correspondent banks")
    correspondents_raw: str | None = Field(None, description="Raw string underlying the list of principal correspondent banks")

    # Miscellaneous
    misc_notes: str | None = Field(None, description="Additional row text including branch notes, advertising, or collection details.")


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")
