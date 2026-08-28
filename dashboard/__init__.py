"""The dashboard, the resource sampler and the terminal report.

None of this is on the decision path: the driver never imports it, and the
documented worst case is that the page breaks while the pipeline keeps running.
Importing `pipeline` from here is the ALLOWED direction — one pick, one
verdict, one threshold table.
"""
