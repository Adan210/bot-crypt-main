import env
from advisor import GeminiAdvisor

def probar_gemini():
    # 1. Cargar configuración del .env
    values = env.load()
    key = values.get('GEMINI_API_KEY')
    
    if not key:
        print("Error: No encontré GEMINI_API_KEY en tu archivo .env. Asegúrate de haberlo creado.")
        return
        
    print("Conectando con Google Gemini API...")
    
    # 2. Inicializar el analista de IA
    modelo = values.get('ADVISOR_MODEL', 'gemini-3.1-flash-lite')
    ai = GeminiAdvisor(key, model=modelo)
    
    # 3. Crear un escenario ficticio (un candidato de compra que el bot le enviaría a la IA)
    candidatos = [{
        'symbol': 'BTCUSDT',
        'regime': 'tendencia_alcista',
        'reason': 'Ruptura fuerte del promedio móvil de 20 periodos con volumen alto',
        'close': 65000.0,
        'atr': 1200.0
    }]
    
    # Contexto ficticio del mercado (Codicia extrema y malas noticias)
    contexto = {
        'fear_greed': 85,  # 85 = Extrema Codicia (Peligroso)
        'funding': {'BTCUSDT': 0.01}, 
        'news': 'Fuertes rumores de nuevas regulaciones estrictas para las criptomonedas en EE. UU.'
    }
    
    print("\n[!] ENVIANDO SITUACIÓN A GEMINI...")
    print(f"-> Operación propuesta: {candidatos[0]['reason']}")
    print(f"-> Contexto del mercado: Miedo/Codicia en {contexto['fear_greed']}, Noticias: {contexto['news']}")
    print("Esperando el análisis de riesgo de la IA...\n")
    
    try:
        # 4. Pedir la decisión a la IA
        decision = ai.review(candidatos, contexto)
        print("=== RESPUESTA Y VEREDICTO DE GEMINI ===")
        
        for moneda, veredicto in decision.items():
            estado = "[OK] APROBADA" if veredicto['approve'] else "[X] RECHAZADA (Veto)"
            print(f"Moneda: {moneda}")
            print(f"Decisión: {estado}")
            print(f"Razón de Gemini: {veredicto['reason']}")
            
    except Exception as e:
        print(f"\nError al contactar a Gemini: {e}")

if __name__ == '__main__':
    probar_gemini()
