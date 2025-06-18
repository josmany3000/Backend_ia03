import os
import openai
import uuid
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import json
import requests
import random
from collections import Counter
import re
import base64 # Importar para codificación Base64
import io # Importar para manejar datos en memoria

# --- CONFIGURACIÓN INICIAL ---
load_dotenv()
app = Flask(__name__)
CORS(app)

# Nueva ruta de verificación de estado para Render
@app.route('/')
def health_check():
    """Ruta simple para que Render verifique que la app está viva."""
    return "Backend IA en funcionamiento", 200

# Inicializa el cliente de OpenAI
try:
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    print("Error: No se pudo inicializar el cliente de OpenAI. Revisa tu clave de API en el archivo .env")
    print(e)

# Clave API de Pixabay
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")

if not PIXABAY_API_KEY:
    print("Advertencia: La clave API de Pixabay no está configurada en el archivo .env. La búsqueda de imágenes/videos de Pixabay podría fallar.")

# AUDIO_DIR ya no es estrictamente necesario para almacenar los MP3 generados,
# pero lo mantenemos por si acaso o para otros usos.
AUDIO_DIR = os.path.join(os.getcwd(), "audio_files")
os.makedirs(AUDIO_DIR, exist_ok=True)

# --- RUTAS DE LA API ---

@app.route('/api/generate-initial-content', methods=['POST'])
def generate_initial_content():
    """
    Endpoint para el Paso 1.
    Recibe la configuración inicial y el guion personalizado,
    lo divide en escenas y genera contenido visual (70% video, 30% imagen) de Pixabay.
    """
    try:
        data = request.json
        guion_personalizado = data.get('guionPersonalizado', '').strip()
        duracion_video_seg = int(data.get('duracion'))
        resolucion = data.get('resolucion')
        nicho = data.get('nicho')
        idioma = data.get('idioma')
        
        if not guion_personalizado:
            return jsonify({"error": "El guion personalizado no puede estar vacío."}), 400

        segundos_por_escena = 7 # Promedio de duración por escena
        num_escenas_esperadas = max(1, round(duracion_video_seg / segundos_por_escena))

        prompt_division = f"""
        Como guionista profesional, tu tarea es dividir el siguiente guion de video en aproximadamente {num_escenas_esperadas} escenas.
        Para cada escena, proporciona:
        - Un guion conciso para la escena.
        - Una lista de 3-5 palabras clave relevantes para buscar contenido visual (imágenes o videos) que ilustre la escena.

        Formato de salida (JSON):
        [
            {{
                "id": "uuid_generado_aqui",
                "script": "Guion de la escena 1...",
                "keywords": ["palabra1", "palabra2", "palabra3"]
            }},
            {{
                "id": "uuid_generado_aqui",
                "script": "Guion de la escena 2...",
                "keywords": ["palabra_a", "palabra_b", "palabra_c"]
            }}
        ]

        Considera el nicho '{nicho}' y el idioma '{idioma}'.
        Guion completo:
        ---
        {guion_personalizado}
        ---
        """
        
        response_gpt = client.chat.completions.create(
            model="gpt-4-turbo",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt_division},
                {"role": "user", "content": guion_personalizado}
            ]
        )
        scenes_from_gpt = json.loads(response_gpt.choices[0].message.content).get('scenes', [])

        final_scenes_data = []

        for i, scene in enumerate(scenes_from_gpt):
            keywords = " ".join(scene.get('keywords', [])[:3])
            
            use_video = random.random() < 0.7 
            
            media_url = None
            if use_video:
                media_url = buscar_video_pixabay(keywords, resolucion)
            
            if not media_url: 
                media_url = buscar_imagen_pixabay(keywords, resolucion)
            
            new_scene = {
                "id": scene.get('id') or str(uuid.uuid4()),
                "script": scene.get('script', ''),
                "imageUrl": media_url if not use_video else None,
                "videoUrl": media_url if use_video else None,
                "audioBase64": None # Ahora enviamos el audio como Base64
            }
            final_scenes_data.append(new_scene)
        
        return jsonify({"scenes": final_scenes_data})

    except Exception as e:
        print(f"Error en generate_initial_content: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/generate-audio', methods=['POST'])
def generate_audio():
    """
    Endpoint para el Paso 3.
    Recibe un conjunto de escenas y una voz, y genera el audio para cada una.
    Descarga el audio de OpenAI y lo devuelve como Base64.
    """
    try:
        data = request.json
        scenes = data.get('scenes', [])
        voice = data.get('voice', 'nova')

        updated_scenes = []
        for scene in scenes:
            # Solo generar si no hay audio o si se solicita regeneración
            if scene.get('script') and not scene.get('audioBase64'):
                response_tts = client.audio.speech.create(
                    model="tts-1",
                    voice=voice,
                    input=scene['script']
                )
                
                # Descargar el contenido del stream a memoria
                audio_buffer = io.BytesIO()
                for chunk in response_tts.iter_bytes(chunk_size=4096):
                    audio_buffer.write(chunk)
                audio_buffer.seek(0) # Volver al inicio del buffer

                # Codificar a Base64
                audio_base64 = base64.b64encode(audio_buffer.read()).decode('utf-8')
                scene['audioBase64'] = audio_base64
            updated_scenes.append(scene)
        
        return jsonify({"scenes": updated_scenes})

    except Exception as e:
        print(f"Error en generate_audio: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/regenerate-scene-part', methods=['POST'])
def regenerate_scene_part():
    """
    Endpoint para el Paso 2 y 3 (regeneración).
    Soporta regeneración de 'script', 'media', o 'audio'.
    Para 'audio', descarga de OpenAI y devuelve como Base64.
    """
    try:
        data = request.json
        part_to_regenerate = data.get('part')
        scene = data.get('scene')
        config = data.get('config')

        if not all([part_to_regenerate, scene, config]):
            return jsonify({"error": "Faltan datos en la petición"}), 400

        if part_to_regenerate == 'script':
            keywords_from_script = extract_keywords(scene.get('script', ''), 3)
            keyword_str = ", ".join(keywords_from_script)

            prompt = f"""
            Eres un guionista experto. Reescribe el siguiente guion para una escena de un video sobre '{config.get('tema') if config.get('tema') else 'un tema relevante'}' en el nicho '{config.get('nicho')}'.
            El tono debe ser {definir_tono_por_nicho(config.get('nicho'))} y en el idioma {config.get('idioma')}.
            Hazlo conciso y potente. Incluye una llamada a la acción o frase de cierre si es apropiado para una escena final.
            Concéntrate en las ideas clave como: {keyword_str}.
            
            GUION ANTIGUO: "{scene.get('script')}"
            
            NUEVO GUION:
            """
            response_gpt = client.chat.completions.create(
                model="gpt-4-turbo",
                messages=[{"role": "system", "content": prompt}]
            )
            new_script = response_gpt.choices[0].message.content.strip()
            return jsonify({"newScript": new_script})

        elif part_to_regenerate == 'media':
            resolucion = config.get('resolucion')
            keywords_for_search = extract_keywords(scene.get('script', ''), 3)
            keyword_str = " ".join(keywords_for_search)
            
            use_video = random.random() < 0.7 
            
            media_url = None
            if use_video:
                media_url = buscar_video_pixabay(keyword_str, resolucion)
            
            if not media_url:
                media_url = buscar_imagen_pixabay(keyword_str, resolucion)
            
            if use_video:
                return jsonify({"newVideoUrl": media_url, "newImageUrl": None})
            else:
                return jsonify({"newImageUrl": media_url, "newVideoUrl": None})

        elif part_to_regenerate == 'audio':
            voice = data.get('voice', 'nova')
            response_tts = client.audio.speech.create(
                model="tts-1", voice=voice, input=scene.get('script')
            )
            
            # Descargar el contenido del stream a memoria y codificar a Base64
            audio_buffer = io.BytesIO()
            for chunk in response_tts.iter_bytes(chunk_size=4096):
                audio_buffer.write(chunk)
            audio_buffer.seek(0)
            audio_base64 = base64.b64encode(audio_buffer.read()).decode('utf-8')

            return jsonify({"newAudioBase64": audio_base64}) # Devolver como newAudioBase64

        else:
            return jsonify({"error": "Parte a regenerar no válida"}), 400

    except Exception as e:
        print(f"Error en regenerate_scene_part: {e}")
        return jsonify({"error": str(e)}), 500
        
@app.route('/api/generate-seo', methods=['POST'])
def generate_seo():
    """
    Endpoint para el Paso 6.
    Recibe el guion completo del video y el nicho, y genera Título, Descripción y Hashtags.
    Ahora soporta regeneración de elementos individuales.
    """
    try:
        data = request.json
        guion = data.get('guion')
        nicho = data.get('nicho')
        seo_type = data.get('type', 'all')

        if not guion or not nicho:
            return jsonify({"error": "Guion y nicho son requeridos para generar SEO"}), 400
        
        seo_data = {}
        if seo_type == 'all' or seo_type == 'titulo':
            prompt_titulo = f"""
            Genera un título corto, viral y que genere curiosidad (máximo 70 caracteres) para un video sobre el nicho '{nicho}'.
            El video trata sobre: "{guion[:200]}..." (primeras 200 caracteres del guion).
            Título:
            """
            response_titulo = client.chat.completions.create(
                model="gpt-4-turbo",
                messages=[{"role": "system", "content": prompt_titulo}]
            )
            seo_data['titulo'] = response_titulo.choices[0].message.content.strip()

        if seo_type == 'all' or seo_type == 'descripcion':
            prompt_descripcion = f"""
            Escribe una descripción optimizada para el algoritmo para un video sobre el nicho '{nicho}'.
            Debe incluir un resumen atractivo del video (basado en el guion), una llamada a la acción (suscribirse, seguir, comentar) y usar palabras clave relevantes del guion.
            Guion: "{guion}"
            Descripción:
            """
            response_descripcion = client.chat.completions.create(
                model="gpt-4-turbo",
                messages=[{"role": "system", "content": prompt_descripcion}]
            )
            seo_data['descripcion'] = response_descripcion.choices[0].message.content.strip()

        if seo_type == 'all' or seo_type == 'hashtags':
            prompt_hashtags = f"""
            Genera una lista de 10-15 hashtags relevantes y populares, mezclando hashtags generales y específicos del nicho '{nicho}' para un video.
            El video trata sobre: "{guion[:200]}..." (primeras 200 caracteres del guion).
            Devuélvelos como un único string separados por espacios (ej: "#finanzas #inversion #bitcoin").
            Hashtags:
            """
            response_hashtags = client.chat.completions.create(
                model="gpt-4-turbo",
                messages=[{"role": "system", "content": prompt_hashtags}]
            )
            seo_data['hashtags'] = response_hashtags.choices[0].message.content.strip()

        return jsonify(seo_data)

    except Exception as e:
        print(f"Error en generate_seo: {e}")
        return jsonify({"error": str(e)}), 500

# La ruta /audio/<filename> ya no es necesaria si todo se maneja en memoria/Base64.
# Si necesitas servir otros archivos en el futuro, puedes mantenerla.
# @app.route('/audio/<filename>')
# def serve_audio(filename):
#     return send_from_directory(AUDIO_DIR, filename)


# --- FUNCIONES AUXILIARES ---
def definir_tono_por_nicho(nicho):
    tonos = {
        "misterio": "enigmático y que genere suspenso",
        "finanzas": "profesional, claro y confiable",
        "tecnologia": "innovador, futurista y fácil de entender",
        "documentales": "informativo, objetivo y narrativo",
        "anime": "entusiasta y conocedor, como un verdadero fan",
        "biblia": "respetuoso, solemne e inspirador",
        "extraterrestres": "misterioso, especulativo y abierto a teorías",
        "tendencias": "moderno, enérgico y llamativo",
        "politica": "objetivo, analítico y equilibrado",
    }
    return tonos.get(nicho, "neutral y atractivo")

def extract_keywords(text, num_keywords=3):
    words = re.findall(r'\b\w+\b', text.lower())
    stopwords = set(["el", "la", "los", "las", "un", "una", "unos", "unas", "de", "en", "y", "o", "es", "para", "con", "del", "al", "se", "por", "que", "más", "como", "pero", "no", "su", "sus", "este", "esta", "estos", "estas", "lo", "mi", "mis"])
    filtered_words = [word for word in words if word not in stopwords and len(word) > 2]
    
    word_counts = Counter(filtered_words)
    most_common = [word for word, count in word_counts.most_common(num_keywords)]
    return most_common if most_common else [text.split(' ')[0]]

def buscar_imagen_pixabay(query, resolution_aspect_ratio):
    orientation = 'all'
    if resolution_aspect_ratio == '9:16':
        orientation = 'vertical'
    elif resolution_aspect_ratio == '16:9':
        orientation = 'horizontal'
    elif resolution_aspect_ratio == '1:1': # Añadir 'square' si Pixabay lo soporta, 'all' es una alternativa
        orientation = 'all' # O 'square' si la API de Pixabay tiene esa opción
    
    url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={query}&image_type=photo&orientation={orientation}&per_page=20"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data['hits']:
            best_image_url = None
            for hit in data['hits']:
                if 'largeImageURL' in hit:
                    best_image_url = hit['largeImageURL']
                    break
                elif 'webformatURL' in hit:
                    best_image_url = hit['webformatURL']
            return best_image_url
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error al buscar imagen en Pixabay: {e}")
        return None

def buscar_video_pixabay(query, resolution_aspect_ratio):
    orientation = 'all'
    if resolution_aspect_ratio == '9:16':
        orientation = 'vertical'
    elif resolution_aspect_ratio == '16:9':
        orientation = 'horizontal'
    elif resolution_aspect_ratio == '1:1':
        orientation = 'all' # O 'square' si la API de Pixabay tiene esa opción

    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&video_type=film&orientation={orientation}&per_page=20"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data['hits']:
            best_video_url = None
            for hit in data['hits']:
                if 'videos' in hit and 'large' in hit['videos'] and 'url' in hit['videos']['large']:
                    best_video_url = hit['videos']['large']['url']
                    break
            return best_video_url
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error al buscar video en Pixabay: {e}")
        return None
