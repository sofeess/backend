from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import os
import time
import pickle
import pandas as pd
import sqlite3

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from requests import Response

from datasource.datasource import get_data_source
from nlp.nlp import NaturalLanguageProcessor
from src.SO.answers import get_answers
from src.config_parameters.technologies import get_all_technologies
from src.details.aggregator import DetailsAggregator
from src.details.details import Details
from src.details.similarity_score_strategy import SimilarityScoreStrategy
from src.SO.update_dump import updateDump
from src.config_parameters.technologies import find_parameter
from train_model import train_model
from urllib import parse

# Path to the pre-trained model
MODEL_PATH = "./BD/model.pickle"
CASSANDRA_PARAMETER_FILE = "./src/config_parameters/cassandra/cassandra_parameters.txt"

load_dotenv()
app = Flask(__name__)

class Dopamine:
    def __init__(self):
        self.load_model()

    def load_model(self):
        print("Loading model...")
        with open(MODEL_PATH, 'rb') as file:
            self.processor: NaturalLanguageProcessor = pickle.load(file)
        print("Model loaded")

    def scheduledUpdate(self):
        input_path = "BD/QueryResults.csv"

        param_file_path = "src/config_parameters/cassandra/cassandra_parameters.txt"

        con = sqlite3.connect("BD/DOPAMine.db")
        cur = con.cursor()

        # get timestamp of last update
        res = cur.execute("SELECT * FROM UpdateStamp ORDER BY UpdateTime DESC LIMIT 1")
        last_update = res.fetchone()[0]

        df_csv = pd.read_csv(input_path)

        current_time = int(time.time())
        updated = updateDump(last_update, current_time, df_csv, input_path, param_file_path)

        # insert new updated timestamp
        exec_str = "INSERT INTO UpdateStamp VALUES(" + str(current_time) + ");"
        res = cur.execute(exec_str)
        con.commit()
        con.close()

        if updated:
            print("Scheduled model update in progress...")
            QUERY_RESULTS_PATH = "BD/QueryResults.csv"
            MODEL_PATH = "BD/model.pickle"

            train_model(
                csv_path=QUERY_RESULTS_PATH,
                output_path=MODEL_PATH
            )
        
            # Model is loaded into NLP object
            self.load_model()
            print("Model updated")

dopamine = Dopamine();

def scheduledUpdate():
    dopamine.scheduledUpdate()

scheduler = BackgroundScheduler()
scheduler.add_job(func=scheduledUpdate, trigger="interval",
                  seconds=int(os.environ['MODEL_UPDATE_INTERVAL_SECONDS']))

scheduler.start()

# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())


@app.route("/")
def home():
    """Basic home route

    Returns:
        str: "Hello, Flask!"
    """
    return "Hello, Flask!"


@app.route("/answers/<question_id>", methods=['GET'])
def answers(question_id: int) -> Response:
    """Fetches answers from question ids.


    Args:
        question_id (int): Id of the question from which answers are fetched.

    Returns:
        Response: Fetched answers.
    """
    print(f"GET /answers/{question_id}")
    answers = get_answers(question_id)
    return jsonify(answers)


@app.route("/technologies", methods=['GET'])
def technologies() -> Response:
    """Fetches all available technologies to search from.

    Returns:
        Response: List of technologies.
    """
    print(f"GET /technologies")
    technologies = get_all_technologies()
    return jsonify(technologies)


@app.route("/search", methods=['GET'])
def search():
    """Searches for configuration parameters based on user query.

    Returns:
        Response: (TODO) Configuration parameters.
    """
    query = request.args.get("q", default="", type=str)
    technology = request.args.get("t", default="", type=str)
    print(f"GET /search?q={query}&t={technology}")

    query = parse.unquote(query)
    technology = parse.unquote(technology)

    # Model is used to determine questions sorted by highest similarity to query and similarity scores
    cosine_similarities, related_indexes = dopamine.processor.search(query, os.environ['SCORE_THRESHOLD'])
    normalized_scores = dopamine.processor.normalize_scores(cosine_similarities, 0, 0.8, 0, 0.9)

    questions = []

    for i in related_indexes:
        index = int(i)
        similarity_score = normalized_scores[index]
        question = dopamine.processor.data_dict[index]
        new_question = {
            "answer_id": question["answer_id"],
            "link": question["link"],
            "parameters": question["parameters"],
            "question_body": question["question_body"],
            "question_id": question["question_id"],
            "question_title": question["question_title"],
            "response_body": question["response_body"],
            "similarity_score": similarity_score,
            "source_name": get_data_source(question["link"]),
            "tags": question["tags"],
        }
        questions.append(new_question)

    aggregator = DetailsAggregator(questions, "parameters")
    aggregated_data = aggregator.aggregate()

    details_list = []

    for parameter in aggregated_data:
        details = Details(
            aggregated_data.get(parameter), 
            parameter, 
            SimilarityScoreStrategy.HIGHEST,
            [
                'answer_id',  'link',  'question_body', 'question_id',
                'question_title', 'response_body', 'similarity_score', 'source_name', 'tags'
            ]
        )
        details_json = details.to_json()
        details_list.append(details_json)

    # Answers are sent as a response
    response = {
        "answers": details_list,
        "query": query,
        "technology": technology
    }

    return jsonify(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)