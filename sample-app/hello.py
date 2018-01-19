from flask import Flask, jsonify
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
import time

app = Flask(__name__)
mongo = MongoClient('mongo', 27017,
                    connectTimeoutMS=1000, socketTimeoutMS=1000, serverSelectionTimeoutMS=1000,
                    appname='demo-app')
db = mongo.sample_database
collection = db.widgets


@app.route('/', methods=['GET'])
def hello():
    try:
        count = collection.count()
        return 'Hello!\nThere are {} items in the database\n'.format(count)
    except ServerSelectionTimeoutError:
        return "Can't connect to MongoDB\n"

@app.route('/add', methods=['POST'])
def add():
    try:
        collection.insert_one({
            'timestamp': int(time.time())
        })
    except ServerSelectionTimeoutError:
        return "Can't connect to MongoDB\n"
    return 'You have added an item to the database!\n'


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)
