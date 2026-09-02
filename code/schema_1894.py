"""Schema for the 1894-1897 Rand McNally directory.

Changes vs. schema_1888:
- New column: Population (extracted into `town_pop`)
- NYC/reserve-city sub-tables expand the balance sheet:
  - NYC commercial: capital, surplus, undivided_profits, individual_deposits, bank_deposits
  - Trust Cos: capital, surplus, undivided_profits, deposits_in_trust, general_deposits
  - Savings: surplus, deposits (Secretary instead of Cashier/Assistant Cashier)
  - Private bankers: address + bank_name + correspondents only
  - Stock Exchange Brokers: name + address in a single cell

The main-table balance sheet still has only capital / surplus / undivided_profits.
The NYC-specific fields are modeled as optional on the same Bank schema.
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
    town_pop: int | None = Field(None, description="Population of the town")
    town_name: str | None = Field(None, description="Name of the town (for NYC-style sub-tables this column is a Street instead; see `address`)")
    county_name: str | None = Field(None, description="County name")

    # Bank Identity (Column 2)
    bank_name: str = Field(..., description="Legal name of the bank")
    old_name: str | None = Field(None, description="Former name of the bank, e.g. 'Valley Bank' from '(Formerly Valley Bank)'")
    address: str | None = Field(None, description="Street address for NYC/reserve-city entries where the 'Town and County' column is replaced by 'Street', or for entries where the address is printed on a second line under the bank name")
    bank_type: BankType = Field("commercial", description="Sub-table the bank comes from: commercial (default), savings, trust, private, investment, broker, foreign")

    # Boolean Flags (Symbols)
    ch_member: int = Field(0, description="1 for Member (*), 2 for Affiliate (+), 0 otherwise")

    # Officers (Columns 3-6)
    president: str | None = Field(None, description="Name of the President")
    vice_president: str | None = Field(None, description="Name of the Vice President")
    cashier: str | None = Field(None, description="Name of the Cashier")
    assistant_cashier: str | None = Field(None, description="Name of the Assistant Cashier")
    secretary: str | None = Field(None, description="Name of the Secretary (savings banks sub-table)")
    treasurer: str | None = Field(None, description="Name of the Treasurer (savings / trust sub-tables)")

    # Bank Events
    event: str | None = Field(None, description="Event such as 'Liquidation', 'Merger with Bank XYZ', 'Closed for liquidation'")
    event_date: str | None = Field(None, description="Date of the event in YYYY-MM-DD format")

    # Liabilities (main-table columns 7-9)
    is_branch: bool = Field(False, description="True if this is a branch of another bank")
    capital: float | None = Field(None, description="Paid-Up Capital")
    surplus: float | None = Field(None, description="Surplus")
    undivided_profits: float | None = Field(None, description="Undivided Profits")

    # NYC/reserve-city extra balance-sheet fields (optional)
    individual_deposits: float | None = Field(None, description="Individual Deposits (NYC commercial sub-table)")
    bank_deposits: float | None = Field(None, description="Bank Deposits (NYC commercial sub-table)")
    deposits_in_trust: float | None = Field(None, description="Deposits in Trust (Trust Companies sub-table)")
    general_deposits: float | None = Field(None, description="General Deposits (Trust Companies sub-table)")
    deposits: float | None = Field(None, description="Deposits (Savings sub-table)")

    # Correspondents (last column)
    correspondents: list[Correspondent] = Field(default_factory=list, description="List of principal correspondent banks")
    correspondents_raw: str | None = Field(None, description="Raw string underlying the list of principal correspondent banks")

    # Miscellaneous
    misc_notes: str | None = Field(None, description="Additional row text including map grid (e.g., 'A 1'), branch notes, or advertising.")


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")
