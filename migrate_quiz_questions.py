"""
One-time migration: replace the purchasing-training quiz questions with the
10 new questions written for the updated slide deck (storage/rent-vs-buy,
SAP, CPR, company card, emergency purchases).

This does NOT touch the module's title, description, pass_mark, or slides —
only the `questions` column. After running this, the new questions are fully
editable through Manage Training in the app, same as any other question.

HOW TO RUN THIS ONCE
---------------------
On Railway, from your project directory with the Railway CLI installed:

    railway run python migrate_quiz_questions.py

Or locally, with your production DATABASE_URL exported first:

    export DATABASE_URL="postgres://...."   # copy from Railway's Variables tab
    python3 migrate_quiz_questions.py

It's safe to run more than once — it always overwrites `questions` with the
list below, so re-running just re-applies the same 10 questions.
"""
import os
import json
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    raise SystemExit(
        "DATABASE_URL is not set. Run this via `railway run python migrate_quiz_questions.py`, "
        "or export DATABASE_URL yourself first."
    )

NEW_QUESTIONS = [
    {
        "question": "What does \"SAP\" stand for in BloomBooks?",
        "options": ["Self Approved Purchase", "Special Authorization Process", "Store Approved Product", "Submit A Purchase"],
        "correct": 0,
        "explanation": "SAP is for something you've already purchased within your approved budget."
    },
    {
        "question": "What does \"CPR\" stand for in BloomBooks?",
        "options": ["Company Product Reimbursement", "Company Purchase Request", "Cash Purchase Receipt", "Certified Purchase Report"],
        "correct": 1,
        "explanation": "A CPR means HWTC buys the item directly on your behalf."
    },
    {
        "question": "Before buying something new for a production, what should you check first?",
        "options": ["Amazon reviews", "The nearest big-box store", "HWTC's existing storage for costumes, props, and set pieces", "Your own closet"],
        "correct": 2,
        "explanation": "HWTC has storage full of items that can often be reused or modified."
    },
    {
        "question": "According to the training, when is renting a good option?",
        "options": ["Never \u2014 HWTC only buys", "For very show-specific items unlikely to be reused", "Only for costumes", "Only if it costs under $20"],
        "correct": 1,
        "explanation": "Show-specific items with no future use are great rental candidates \u2014 storage and budget are both limited."
    },
    {
        "question": "Who should you contact with questions about HWTC's inventory?",
        "options": ["The Treasurer", "The President", "Asset Manager Jillian", "Any board member"],
        "correct": 2,
        "explanation": "Jillian, HWTC's Asset Manager, handles inventory questions."
    },
    {
        "question": "What does the training say is the org's preferred way to get an item to a production, when possible?",
        "options": ["Always use a Company Purchase Request", "Purchase it yourself and submit a SAP", "Always use the HWTC credit card", "Wait for board approval"],
        "correct": 1,
        "explanation": "Self-purchasing is often fastest and lets you keep any rewards or cash back \u2014 submit a SAP to get reimbursed."
    },
    {
        "question": "What must be attached to every SAP submission?",
        "options": ["A vendor invoice", "Nothing if it's under $20", "Your original receipt", "A bank statement"],
        "correct": 2,
        "explanation": "No receipt, no reimbursement \u2014 this applies to every SAP regardless of amount."
    },
    {
        "question": "What happens once a Company Purchase Request is approved?",
        "options": ["You purchase it yourself and get reimbursed", "HWTC makes the purchase directly, and your producer notifies you when it's done", "A company card is mailed to you", "Nothing further happens"],
        "correct": 1,
        "explanation": "Unlike a SAP, HWTC completes the purchase \u2014 you never pay out of pocket."
    },
    {
        "question": "Who can decide whether a volunteer is issued a company card?",
        "options": ["Any board member", "The volunteer themselves, after training", "The production's producer or the Board Treasurer", "The Asset Manager"],
        "correct": 2,
        "explanation": "Card access is restricted and not automatic \u2014 it's authorized by the producer or Treasurer."
    },
    {
        "question": "What are last-minute purchases made during tech week or performances called in BloomBooks?",
        "options": ["Rush Requests", "Late Purchases", "Board Overrides", "Emergency Purchases"],
        "correct": 3,
        "explanation": "These must be made by an eligible Board Member and are tracked in BloomBooks as Emergency Purchases."
    },
]


def main():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    cur.execute("SELECT id, title, questions FROM bb_training_modules WHERE is_active=1 LIMIT 1")
    module = cur.fetchone()
    if not module:
        print("No active training module found \u2014 nothing to update.")
        conn.close()
        return

    old_count = len(json.loads(module['questions'] or '[]'))
    print(f"Found active module #{module['id']} ({module['title']!r}) with {old_count} existing question(s).")

    cur.execute(
        "UPDATE bb_training_modules SET questions=%s WHERE id=%s",
        (json.dumps(NEW_QUESTIONS), module['id'])
    )
    conn.commit()
    conn.close()
    print(f"Done \u2014 module #{module['id']} now has {len(NEW_QUESTIONS)} question(s).")
    print("You can review/edit them any time in the app under Manage Training.")


if __name__ == '__main__':
    main()
