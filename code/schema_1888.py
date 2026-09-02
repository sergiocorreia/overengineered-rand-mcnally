"""Schema for the 1888-1893 Rand McNally directory.

Changes vs. schema_1879:
- New column: Undivided Profits
- New columns: Vice-President, Assistant Cashier
- All correspondents are now in a single column (merged by the publisher)

Reserve-city sub-tables (NYC and similar) use the same `bank_type` /
`address` / `secretary` / `treasurer` mapping as in 1879. See prompt_1888.md
for details.
"""

from typing import Literal

from pydantic import BaseModel, Field

from schema_1911 import Correspondent


BankType = Literal[
    "commercial",
    "savings",
    "trust",
    "private",
    "investment",
    "broker",
    "foreign",
]


class Bank(BaseModel):
    state_abbreviation: str | None = Field(None, description="Two-digit state abbreviation detected from table header (e.g., RI)")

    # Location Info (Column 1)
    town_name: str | None = Field(None, description="Name of the town (for NYC-style sub-tables this column is a Street instead; see `address`)")
    county_name: str | None = Field(None, description="County name")

    # Bank Identity (Column 2)
    bank_name: str = Field(..., description="Legal name of the bank")
    old_name: str | None = Field(None, description="Former name of the bank, e.g. 'Valley Bank' from '(Formerly Valley Bank)'")
    address: str | None = Field(None, description="Street address for NYC/reserve-city entries where the 'Town and County' column is replaced by 'Street'")
    bank_type: BankType = Field("commercial", description="Sub-table the bank comes from: commercial (default), savings, trust, private, investment, broker, foreign")

    # Boolean Flags (Symbols)
    ch_member: int = Field(0, description="1 for Member (*), 2 for Affiliate (+), 0 otherwise")

    # Officers (Columns 3-6)
    president: str | None = Field(None, description="Name of the President")
    vice_president: str | None = Field(None, description="Name of the Vice President")
    cashier: str | None = Field(None, description="Name of the Cashier")
    assistant_cashier: str | None = Field(None, description="Name of the Assistant Cashier")
    secretary: str | None = Field(None, description="Name of the Secretary (savings banks sub-table)")
    treasurer: str | None = Field(None, description="Name of the Treasurer (savings banks sub-table)")

    # Bank Events (for closed/liquidated/merged banks)
    event: str | None = Field(None, description="Event such as 'Liquidation', 'Merger with Bank XYZ', 'Closed for liquidation'")
    event_date: str | None = Field(None, description="Date of the event in YYYY-MM-DD format")

    # Liabilities (Columns 7-9)
    is_branch: bool = Field(False, description="True if this is a branch of another bank")
    capital: float | None = Field(None, description="Paid-Up Capital")
    surplus: float | None = Field(None, description="Surplus")
    undivided_profits: float | None = Field(None, description="Undivided Profits")

    # Correspondents (Column 10)
    correspondents: list[Correspondent] = Field(default_factory=list, description="List of principal correspondent banks")
    correspondents_raw: str | None = Field(None, description="Raw string underlying the list of principal correspondent banks")

    # Miscellaneous
    misc_notes: str | None = Field(None, description="Additional row text including map grid (e.g., 'A 1'), branch notes, or advertising.")


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")
