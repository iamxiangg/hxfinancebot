from __future__ import annotations

REVENUE_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:SalesRevenueGoodsNet",
    "us-gaap:SalesRevenueServicesNet",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
)

COST_OF_REVENUE_CONCEPTS = (
    "us-gaap:CostOfRevenue",
    "us-gaap:CostOfGoodsAndServicesSold",
    "us-gaap:CostOfGoodsSold",
    "us-gaap:CostOfServices",
)

GROSS_PROFIT_CONCEPTS = (
    "us-gaap:GrossProfit",
)

OPERATING_INCOME_CONCEPTS = (
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
)

NET_INCOME_CONCEPTS = (
    "us-gaap:NetIncomeLoss",
    "us-gaap:ProfitLoss",
    "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
)

OPERATING_CASH_FLOW_CONCEPTS = (
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)

CAPEX_CONCEPTS = (
    "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
    "us-gaap:PaymentsToAcquireProductiveAssets",
)

CASH_CONCEPTS = (
    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "us-gaap:CashCashEquivalentsAndShortTermInvestments",
    "us-gaap:CashAndCashEquivalentsFairValueDisclosure",
)

SHORT_TERM_INVESTMENTS_CONCEPTS = (
    "us-gaap:ShortTermInvestments",
    "us-gaap:MarketableSecuritiesCurrent",
)

TOTAL_DEBT_CONCEPTS = (
    "us-gaap:LongTermDebt",
    "us-gaap:LongTermDebtAndCapitalLeaseObligations",
    "us-gaap:DebtCurrent",
    "us-gaap:LongTermDebtCurrent",
)

ACCOUNTS_RECEIVABLE_CONCEPTS = (
    "us-gaap:AccountsReceivableNetCurrent",
    "us-gaap:AccountsAndNotesReceivableNet",
    "us-gaap:AccountsReceivableNet",
)

INVENTORY_CONCEPTS = (
    "us-gaap:InventoryNet",
    "us-gaap:InventoryFinishedGoodsNetOfReserves",
)

DILUTED_SHARES_CONCEPTS = (
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap:WeightedAverageNumberOfSharesOutstandingDiluted",
)

STOCK_BASED_COMP_CONCEPTS = (
    "us-gaap:ShareBasedCompensation",
    "us-gaap:AllocatedShareBasedCompensationExpense",
    "us-gaap:StockBasedCompensation",
)

TOTAL_DEBT_COMBINED_CONCEPTS = (
    "us-gaap:LongTermDebtAndCapitalLeaseObligations",
    "us-gaap:LongTermDebt",
    "us-gaap:LongTermDebtCurrent",
    "us-gaap:DebtCurrent",
    "us-gaap:LongTermDebtNoncurrent",
    "us-gaap:ShortTermBorrowings",
    "us-gaap:CommercialPaper",
)


def pick_best_fact(facts: list, *, prefer_last: bool = True) -> dict | None:
    if not facts:
        return None
    if prefer_last:
        return facts[-1]
    return facts[0]
