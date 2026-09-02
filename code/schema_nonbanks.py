"""Schema for non-bank tables in the Rand McNally directory.

Covers the specialized reserve-city sub-tables that have NO balance-sheet
columns: private bankers, savings banks listed in their own sub-table,
trust companies (when listed without balance sheet), foreign banking
agencies, stock-exchange members, investment dealers / brokers / finance
companies / acceptance corporations, and similar. The main commercial-bank
schemas (schema_1879 ... schema_1934) cover everything else.

A single unified schema is used across all eras; the prompt describes the
several layout styles (2-col name+location grid, 4-col grid of pairs,
multi-column firm listings) and tells the LLM how to map each onto these
fields.
"""

from typing import Literal

from pydantic import BaseModel, Field

from schema_1911 import Correspondent


BankType = Literal[
    "commercial",      # rare, but allowed (catches mis-classification)
    "savings",
    "trust",
    "private",
    "investment",      # investment bankers / dealers
    "broker",          # stock-exchange members, security dealers
    "foreign",         # foreign banking agencies
    "finance",         # finance companies, acceptance corporations
    "clearinghouse",
    "other",
]


class Bank(BaseModel):
    state: str | None = Field(None, description="Two-letter state abbreviation extracted from the page header (e.g. 'NY' for 'NEW YORK CITY')")
    city: str | None = Field(None, description="City extracted from the page header (e.g. 'New York' for 'NEW YORK CITY-continued.')")

    bank_name: str = Field(..., description="Name of the institution (banker, firm, or company)")
    address: str | None = Field(None, description="Street address as printed (e.g. '6 Hanover', '(822 Baltimore Ave.)')")
    bank_type: BankType = Field(
        "other",
        description=(
            "Inferred from the section heading: 'PRIVATE BANKERS' -> 'private'; 'SAVINGS BANKS' -> 'savings'; "
            "'TRUST COMPANIES' -> 'trust'; 'MEMBERS OF STOCK EXCHANGE' / 'STOCK EXCHANGE BROKERS' -> 'broker'; "
            "'INVESTMENT BANKERS' / 'INVESTMENT DEALERS' -> 'investment'; 'FOREIGN BANKING AGENCIES' -> 'foreign'; "
            "finance companies / acceptance corporations -> 'finance'; clearinghouse rosters -> 'clearinghouse'; "
            "anything else -> 'other'"
        ),
    )
    classification: str | None = Field(
        None,
        description=(
            "Verbatim sub-heading or inline label printed near the entry, e.g. "
            "'UNDERWRITERS AND DISTRIBUTORS OF INVESTMENT SECURITIES', 'MUNICIPAL AND CORPORATION BONDS', "
            "'SECURITY DEALERS', 'INVESTMENT SECURITIES', 'COMMERCIAL PAPER'"
        ),
    )
    established_year: int | None = Field(
        None,
        description=(
            "Year established. 4 digits (e.g. 1905) stored as is; 2 digits preceded by an apostrophe "
            "(e.g. \"'29\") stored as the 2-digit integer (29) without expansion."
        ),
    )

    officers: str | None = Field(
        None,
        description=(
            "Semicolon-delimited 'Name, Role' pairs (e.g. 'John Smith, Pres.; Jane Doe, V. Pres.'). "
            "Roles use the abbreviations from the source: Pres., V. Pres., Sec., Treas., Mgr., Partner, etc."
        ),
    )
    exchange_memberships: str | None = Field(
        None,
        description=(
            "Semicolon-delimited verbatim memberships, e.g. 'Mem. Am. Stk. Ass.; Kan. State Stk. Ass.; Mem. Inv. Br. Ass.'"
        ),
    )
    branches: str | None = Field(
        None,
        description=(
            "Semicolon-delimited list of branch / affiliated office locations, e.g. 'Omaha, Nebr.; St. Joseph, Mo.' "
            "or 'Phila.; Bos.' from a parenthetical like '(Also Phila. and Bos.)'"
        ),
    )

    correspondents: list[Correspondent] = Field(
        default_factory=list,
        description="List of correspondent banks / depositories, when listed for the entry",
    )
    correspondents_raw: str | None = Field(
        None,
        description="Raw correspondents-cell text, preserved for validation",
    )

    misc_notes: str | None = Field(
        None,
        description=(
            "Anything else: advertisement copy, taglines, teletype IDs (e.g. 'A.T.&T. Teletype K.C. 188'), "
            "'See Advertisement opposite Title Page', etc. Multiple notes joined with semicolons."
        ),
    )


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other non-tabular information")
    banks: list[Bank] = Field(..., description="List of non-bank institutions found on the page")
