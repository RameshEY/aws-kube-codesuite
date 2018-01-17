from flask import Flask, jsonify
from pymongo import MongoClient
import time

app = Flask(__name__)
mongo = MongoClient('mongo', 27017)
db = mongo.sample_database
collection = db.widgets

@app.route('/', methods=['GET'])
def hello():
    return 'Hello!\nThere are {} items in the database'.format(collection.count())

@app.route('/add', methods=['POST'])
def add():
    collection.insert_one({
        'timestamp': int(time.time())
    })
    return 'You have added an item to the database!'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)
