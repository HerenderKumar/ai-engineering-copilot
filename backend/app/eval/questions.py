EVAL_QUESTIONS = [
    {
        "question": "What is the main entry point of this project?",
        "expect_sources": True
    },
    {
        "question": "Which files define API routes?",
        "expect_sources": True
    },
    {
        "question": "Does this project use Kafka?",
        "expect_sources": False  # should refuse
    }
]
