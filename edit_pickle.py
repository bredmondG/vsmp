import pickle
with open('progress.pkl', 'rb') as f:
    data = pickle.load(f)


data['frame'] = 200
print(data['frame'])

with open('progress.pkl', 'wb') as f:
    data = pickle.dump(data, f)