"""Schema for the 1879-1887 Rand McNally directory.

Columns vs. the 1911 schema:
- No population column, no vice-president, no assistant cashier
- No symbols other than the clearinghouse asterisk (*)
- No established year, no transit/charter number
- Balance sheet limited to Paid-up Capital and Surplus
- Two separate correspondent columns (NY and Chicago/other) merged into a
  single `correspondents` list, preserving both raw strings via `correspondents_raw`

Reserve-city sub-tables (NYC and similar) are represented with the shared
`bank_type` field and, when relevant, the `address` / `secretary` / `treasurer`
fields (see prompt_1879.md for the mapping rules).
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

    # Officers (Columns 3-4)
    president: str | None = Field(None, description="Name of the President")
    cashier: str | None = Field(None, description="Name of the Cashier")
    secretary: str | None = Field(None, description="Name of the Secretary (savings banks sub-table)")
    treasurer: str | None = Field(None, description="Name of the Treasurer (savings banks sub-table)")

    # Bank Events (for closed/liquidated/merged banks)
    event: str | None = Field(None, description="Event such as 'Liquidation', 'Merger with Bank XYZ', 'Closed for liquidation'")
    event_date: str | None = Field(None, description="Date of the event in YYYY-MM-DD format")

    # Liabilities (Columns 5-6)
    is_branch: bool = Field(False, description="True if this is a branch of another bank")
    capital: float | None = Field(None, description="Paid-Up Capital")
    surplus: float | None = Field(None, description="Surplus")

    # Correspondents (Columns 7-8, merged into a single list)
    correspondents: list[Correspondent] = Field(default_factory=list, description="Merged list of correspondent banks from the NY and Chicago/other correspondent columns")
    correspondents_raw: str | None = Field(None, description="Raw strings from both correspondent columns, concatenated with ' | ' in left-to-right column order (column 7 text first, then column 8 text)")

    # Miscellaneous
    misc_notes: str | None = Field(None, description="Additional row text including map grid (e.g., 'A 1'), branch notes, or advertising.")


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")
