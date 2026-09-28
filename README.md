# RupyaVasool - Transform Abandoned Carts into Loyal Customers

An interactive checkout-recovery demo. It shows how a store can respond to payment issues and abandoned carts with a customer-selected reason, a bounded suggestion, opt-out controls, and separate organic versus assisted outcomes.

## Open the public demo with GitHub Pages

The static site is in [`docs/index.html`](docs/index.html). It runs in the visitor's browser and does not need a terminal or local server.

1. Push the repository changes to GitHub.
2. In the repository, open **Settings → Pages**.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Choose branch **main** and folder **/docs**, then save.
5. After GitHub finishes publishing, open <https://ggunjan229.github.io/Revenue-Recovery/>. Add that URL to the repository's **About → Website** field if you want a visible project link.

The Pages demo simulates purchases, suggestions, and the audit feed using browser local storage. The data stays in that browser and is not shared with the store or other visitors. It sends no messages and makes no real payments. Use fake details. The scheduled batch worker is available only in the local FastAPI version.

## Run the full local Python demo

The FastAPI app and APScheduler worker use SQLite and Gemini fallback copy. This version does require running Python locally; GitHub Pages cannot run the API, database, or worker.

From Windows PowerShell in the repository folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_store.py
```

Open <http://127.0.0.1:8000>. In a second terminal, start the recovery worker:

```powershell
.\.venv\Scripts\python.exe run_worker.py
```

The local demo uses fallback messages if `GEMINI_API_KEY` is blank. To use Gemini, put a valid key in your private `.env`; never commit `.env`. `revenue_recovery.db` is created automatically when the app starts if it does not exist.

## Recovery policy shown by the local app

- Pending checkouts wait through a grace period; failed payments can be considered sooner.
- Outbound email requires explicit consent, respects opt-outs and configured quiet hours, and stops after three successful sends.
- Every reason, send, stop, and demo recovery is recorded in an audit trail.
- An agent-assisted conversion is counted only when a shopper completes the demo recovery flow. This is workflow attribution, not proof that the agent caused incremental revenue.
- Price comparison is a reason category only; the demo does not fetch competitor prices.

## Security

`.env` and SQLite database files are ignored by Git. The public repository previously tracked these files. Rotate any real credentials that were in `.env`; removing a file in a new commit does not erase it from old Git history. Review repository history before sharing further.
