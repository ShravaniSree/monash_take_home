"""Northstar accounts-payable processing agent.

Layers, from the inside out:

* ``domain`` holds the rules and the data contracts.
* ``retrieval`` holds the policy index.
* ``systems`` holds the simulated vendor, purchase-order, history, and ledger.
* ``application`` runs one case through evidence, reconciliation, and approval.
* ``api`` and ``cli`` are the interfaces over that application.
* ``utils`` holds shared path helpers.
"""

__version__ = "0.1.0"
