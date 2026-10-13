from flask import Flask, render_template_string, request, send_from_directory
import os

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# HTML Template dengan antara muka pemilihan fail dan pemain
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Artemis Engine Web Player</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #121212;
            color: #e0e0e0;
            margin: 0;
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        h1 {
            color: #bb86fc;
            margin-bottom: 10px;
        }
        .container {
            background: #1e1e1e;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.5);
            width: 100%;
            max-width: 800px;
            text-align: center;
        }
        input[type="file"] {
            margin: 15px 0;
            padding: 10px;
            background: #2d2d2d;
            border: 1px dashed #bb86fc;
            border-radius: 5px;
            color: #fff;
            width: 80%;
            cursor: pointer;
        }
        button {
            background-color: #bb86fc;
            color: #121212;
            border: none;
            padding: 10px 20px;
            font-weight: bold;
            border-radius: 5px;
            cursor: pointer;
            transition: background 0.3s;
        }
        button:hover {
            background-color: #3700b3;
            color: #fff;
        }
        #player-container {
            margin-top: 20px;
            width: 100%;
            height: 450px;
            background: #000;
            border-radius: 5px;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
        }
        #game-canvas {
            width: 100%;
            height: 100%;
        }
        .placeholder-text {
            color: #757575;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Artemis Engine Web Player</h1>
        <p>Pilih folder atau fail permainan Artemis Engine anda untuk mula bermain.</p>
        
        <form id="upload-form" action="/load-game" method="POST" enctype="multipart/form-data">
            <input type="file" name="game_files" id="game_files" webkitdirectory directory multiple required><br>
            <button type="submit">Muat & Main Game</button>
        </form>

        <div id="player-container">
            <div class="placeholder-text" id="status-text">Sila muat naik fail game terlebih dahulu...</div>
            <!-- Canvas atau elemen game akan dipaparkan di sini -->
        </div>
    </div>

    <script>
        const form = document.getElementById('upload-form');
        const statusText = document.getElementById('status-text');

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(form);
            
            statusText.textContent = "Memuat naik dan memproses fail...";
            
            const response = await fetch('/load-game', {
                method: 'POST',
                body: formData
            });
            
            const result = await response.json();
            if (result.success) {
                statusText.textContent = "Fail dimuat naik! Menginisialisasi Artemis Engine...";
                // Di sini integrasi skrip runtime Artemis Engine JS/WASM boleh dipanggil
            } else {
                statusText.textContent = "Ralat: " + result.error;
            }
        });
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/load-game', methods=['POST'])
def load_game():
    if 'game_files' not in request.files:
        return jsonify({'success': False, 'error': 'Tiada fail dipilih.'})
    
    files = request.files.getlist('game_files')
    for file in files:
        if file.filename:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            file.save(file_path)
            
    return jsonify({'success': True, 'message': 'Fail berjaya dimuat naik ke pelayan.'})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
