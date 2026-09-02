"""Schema to extract bank information from the Rand McNally directory."""

from pydantic import BaseModel, Field

class Correspondent(BaseModel):
    name: str = Field(..., description="Name of the correspondent bank")
    city: str | None = Field(None, description="City of the correspondent bank")
    state: str | None = Field(None, description="Two-digit state abbreviation of the correspondent bank")

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

    # Liabilities (Columns 7-10)
    # These are Optional because Branch banks often reference the Home Office instead of listing figures
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

    # Added field for miscellaneous row text
    misc_notes: str | None = Field(None, description="Additional row text including branch notes, advertising, or collection details.")

class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")

