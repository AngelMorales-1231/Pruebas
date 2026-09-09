def enqueue(f, *args, **kwargs):
    # Función mock para simular la cola de tareas en Vercel
    try:
        return f(*args, **kwargs)
    except Exception:
        pass
