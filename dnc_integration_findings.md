# Hosted DNC cleaner integration findings

The user-provided service is `https://csv-cleaner-app-uc0p.onrender.com/`.

The public interface is a Streamlit app titled **CSV Cleaner**. It visibly provides:

- An upload control for one or more CSV files to clean, with a displayed limit of 200MB per file.
- An optional upload control for additional suppression CSV files.
- A checkbox labeled **Scrub phone numbers against TCPA Litigator List**, enabled by default in the observed state.
- A **Run Cleaning** button.
- A public health endpoint at `https://csv-cleaner-app-uc0p.onrender.com/_stcore/health` that returned `ok`.

The provided interface attachment showed internal DNC lists for Emails.csv, Phones.csv, and Domains.csv, plus a TCPA phone scrub pass. The page is a Streamlit interactive frontend; the observed public HTML did not expose a documented REST upload/clean endpoint. A robust desktop integration therefore needs either a documented API endpoint supplied by the service owner or an approved browser automation adapter. Uploading user contact files to the hosted service must be optional and explicitly visible because data leaves the local computer.
