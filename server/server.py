from flask import Flask, render_template
print("hello")

app = Flask(__name__)

def display_logs():
    with open('../log.txt', 'r') as log_file:
        logs = log_file.readlines()
    return logs[-10:]

@app.route('/')
def home():
    frame = "current_frame.jpg"
    return render_template('i_logs.html', log_lines = display_logs(), image_filename=frame)

@app.route('/frame')
def display_frame():
    frame = "current_frame.jpg"
    return render_template('frame.html', image_filename=frame)


def hello_world():
    return "hello world"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)