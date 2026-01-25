import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from backend.models import Base, Staff, User
from backend.media_manager import create_required_directories

# путь до корня проекта
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# путь до database/dance.db
DB_PATH = os.path.join(BASE_DIR, "database", "dance.db")
print("DB PATH:", DB_PATH)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False
)

Session = sessionmaker(bind=engine)

def init_db():
    # Создаем необходимые папки
    create_required_directories()
    # Создаем таблицы
    Base.metadata.create_all(engine)
    # Инициализируем admin и owner
    init_admin_and_owner()

def init_admin_and_owner():
    """
    Инициализирует технического админа и всех владельцев в БД.
    Если их нет - добавляет, если есть - обновляет должность.
    Имена автоматически подгружаются из профилей пользователей.
    """
    from config import OWNER_IDS, TECH_ADMIN_ID
    
    db = Session()
    
    try:
        # Инициализируем технического админа (если указан)
        if TECH_ADMIN_ID:
            tech_admin = db.query(Staff).filter_by(telegram_id=TECH_ADMIN_ID).first()
            tech_admin_name = "Технический админ"
            
            # Подгружаем имя из профиля пользователя
            user = db.query(User).filter_by(telegram_id=TECH_ADMIN_ID).first()
            if user and user.name:
                tech_admin_name = user.name
            
            if not tech_admin:
                tech_admin = Staff(
                    name=tech_admin_name,
                    phone=None,
                    telegram_id=TECH_ADMIN_ID,
                    position="тех. админ",
                    status="active"
                )
                db.add(tech_admin)
                print(f"✅ Создан технический админ (ID: {TECH_ADMIN_ID}, имя: {tech_admin_name})")
            else:
                if tech_admin.position != "тех. админ":
                    tech_admin.position = "тех. админ"
                    print(f"🔄 Обновлена должность тех. админа")
                if (not tech_admin.name or tech_admin.name.strip() == "") and user and user.name:
                    tech_admin.name = tech_admin_name
                    print(f"🔄 Заполнено имя тех. админа из профиля")
        
        # Инициализируем всех владельцев
        for idx, owner_id in enumerate(OWNER_IDS, 1):
            owner = db.query(Staff).filter_by(telegram_id=owner_id).first()
            owner_name = f"Владелец {idx}" if len(OWNER_IDS) > 1 else "Владелец"
            
            # Подгружаем имя из профиля пользователя
            user = db.query(User).filter_by(telegram_id=owner_id).first()
            if user and user.name:
                owner_name = user.name
            
            if not owner:
                owner = Staff(
                    name=owner_name,
                    phone=None,
                    telegram_id=owner_id,
                    position="владелец",
                    status="active"
                )
                db.add(owner)
                print(f"✅ Создан владелец (ID: {owner_id}, имя: {owner_name})")
            else:
                if owner.position != "владелец":
                    owner.position = "владелец"
                    print(f"🔄 Обновлена должность владельца (ID: {owner_id})")
                if (not owner.name or owner.name.strip() == "") and user and user.name:
                    owner.name = owner_name
                    print(f"🔄 Заполнено имя владельца из профиля (ID: {owner_id})")
        
        db.commit()
        print("✅ Инициализация персонала завершена")
    except Exception as e:
        db.rollback()
        print(f"❌ Ошибка при инициализации персонала: {e}")
    finally:
        db.close()

def get_session():
    return Session()
