# -*- coding: utf-8 -*-
"""One-off generator for the seed vocabulary CSVs (config/vocab/*.csv)."""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "config" / "vocab"

def rows(name: str, data: list[tuple[str, str, str]]) -> None:
    lines = ["value,category,variants"]
    for value, category, variants in data:
        v = variants.replace('"', '""')
        lines.append(f'{value},{category},"{v}"')
    (OUT / f"{name}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{name}.csv: {len(data)} entries")

rows("capability", [
    # (canonical, category, variants)
    ("Python", "programming_language", "پایتون|py|python3|python 3|پیتون"),
    ("SQL", "language", "اس‌کیو‌ال|اسکیوال| sequel"),
    ("NoSQL", "language", "نو‌اس‌کیو‌ال|نوسکیوال"),
    ("Java", "programming_language", "جاوا"),
    ("JavaScript", "programming_language", "جاوا اسکریپت|جاوااسکریپت|js"),
    ("TypeScript", "programming_language", "تایپ اسکریپت|تایپ‌اسکریپت|ts"),
    ("C#", "programming_language", "سی شارپ|سی‌شارپ|csharp|c sharp"),
    ("C++", "programming_language", "سی پلاس پلاس|سی‌پلاس‌پلاس"),
    ("Go", "programming_language", "گو|golang"),
    ("Rust", "programming_language", "راست"),
    ("PHP", "programming_language", "پی اچ پی|پی‌اچ‌پی"),
    ("R", "language", "زبان r|آر"),
    ("Julia", "language", "جولیا|جولیا"),
    ("Django", "framework", "جنگو"),
    ("Flask", "framework", "فلسک"),
    ("FastAPI", "framework", "فست‌اپی‌آی|fast api"),
    ("React", "framework", "ری‌اکت|ریاکت|react.js|reactjs"),
    ("Vue", "framework", "ویو|vue.js|vuejs|ویو جی اس"),
    ("Angular", "framework", "انگولار|angularjs"),
    ("Node.js", "framework", "نود جی اس|نود‌جی‌اس|nodejs|node"),
    ("Next.js", "framework", "نکست جی اس|nextjs|نکست‌جی‌اس"),
    ("Laravel", "framework", "لاراول"),
    ("Spring Boot", "framework", "اسپرینگ بوت|spring"),
    ("TensorFlow", "library", "تنسورفلو|تنسور فلو|tf"),
    ("PyTorch", "library", "پای‌تاچ|پایتورچ|torch"),
    ("Keras", "library", "کراس"),
    ("Scikit-learn", "library", "scikit learn|اسکیت لرن|اسکیکیت‌لرن|sklearn"),
    ("Pandas", "library", "پانداس"),
    ("NumPy", "library", "نامپای|نمپی"),
    ("Hugging Face", "platform", "هاگینگ فیس|huggingface|هیوگینگ فیس|ترنسفورمرز|transformers"),
    ("LangChain", "framework", "لنگ چین|لنگ‌چین|lang chain"),
    ("LlamaIndex", "framework", "لاما ایندکس|لماینکس|llama index"),
    ("LangGraph", "framework", "لنگ گراف|لنگ‌گراف"),
    ("CrewAI", "framework", "کرو ای‌آی|crew ai"),
    ("AutoGen", "framework", "اتوجن|auto gen"),
    ("n8n", "tool", "ان‌ایت|n 8 n"),
    ("Docker", "tool", "داکر"),
    ("Kubernetes", "tool", "کوبرنتیز|k8s|کوبرنتیز"),
    ("Git", "tool", "گیت"),
    ("GitHub", "tool", "گیت هاب|گیت‌هاب"),
    ("GitLab", "tool", "گیت لب|گیت‌لب"),
    ("Linux", "platform", "لینوکس"),
    ("PostgreSQL", "database", "پستگرس|postgre sql|پستگرس‌کیو‌ال|postgres"),
    ("MySQL", "database", "مای اسکیو ال|مای‌اس‌کیو‌ال"),
    ("MongoDB", "database", "مانگو دی بی|مانگودی‌بی|mongo"),
    ("Redis", "database", "ردیس"),
    ("Elasticsearch", "database", "الاستیک سرچ|elastic"),
    ("SQL Server", "database", "اسکیو ال سرور|mssql|سی‌کوئل سرور"),
    ("SQLite", "database", "اس‌کیو‌لایت"),
    ("Qdrant", "tool", "کوادرانت|کادرانت"),
    ("Pinecone", "tool", "پاین‌کون"),
    ("Milvus", "tool", "میویوس|ملویس"),
    ("Weaviate", "tool", "ویاویت"),
    ("ChromaDB", "tool", "کروما|chroma"),
    ("FAISS", "tool", "فیس|faiss"),
    ("Snowflake", "platform", "اسنوفلیک"),
    ("Spark", "platform", "اسپارک|apache spark"),
    ("Kafka", "platform", "کافکا|apache kafka"),
    ("Airflow", "tool", "ایرفلو|apache airflow"),
    ("MLflow", "tool", "ام‌ال‌فلو|ml flow"),
    ("Databricks", "platform", "دیتابریکس"),
    ("Power BI", "tool", "پاور بی آی|پاوربی‌آی|powerbi|پاور بی‌آی"),
    ("Tableau", "tool", "تبلو"),
    ("Microsoft Excel", "tool", "اکسل|excel|مایکروسافت اکسل"),
    ("Google Sheets", "tool", "گوگل شیت|گوگل‌شیت"),
    ("AWS", "platform", "amazon web services|امازون وب سرویس|ای‌دبلیو‌اس"),
    ("Azure", "platform", "آژور|مایکروسافت آژور"),
    ("Google Cloud", "platform", "gcp|گوگل کلود|گوگل کلاود"),
    ("OpenCV", "library", "اوپن سی وی|اوپن‌سی‌وی"),
    ("YOLO", "model", "یولو"),
    ("Whisper", "model", "ویسپر"),
    ("OpenAI API", "platform", "openai|اوپن ای‌آی|جی‌پی‌تی api|gpt api"),
    ("Azure OpenAI", "platform", "azure open ai"),
    ("Ollama", "tool", "الاما|اولاما"),
    ("vLLM", "tool", "وی ال ال ام|vllm"),
    ("Machine Learning", "concept", "یادگیری ماشین|یادگیری‌ماشین|machine learning"),
    ("Deep Learning", "concept", "یادگیری عمیق|یادگیری‌عمیق|دیپ لرنینگ"),
    ("NLP", "concept", "پردازش زبان طبیعی|nlp|پردازش زبان‌های طبیعی"),
    ("Computer Vision", "concept", "بینایی ماشین|بینایی کامپیوتر|c v|cv"),
    ("LLM", "concept", "ال ال ام|مدل زبانی بزرگ|large language model|مدل‌های زبانی بزرگ"),
    ("RAG", "concept", "راگ|بازیابی افزایش‌یافته|retrieval augmented generation"),
    ("Agentic AI", "concept", "ایجنت ای‌آی|عاملی|ai agent ها|هوش مصنوعی عاملی|agent based ai"),
    ("AI Agents", "concept", "ایجنت|عامل هوشمند|ai agent"),
    ("Prompt Engineering", "concept", "مهندسی پرامپت|پرامپت نویسی|prompt engineering"),
    ("Fine-tuning", "technique", "فاین تیون|فاین‌تیونینگ|فاین تیونینگ|finetuning|fine tuning"),
    ("Reinforcement Learning", "technique", "یادگیری تقویتی|rl"),
    ("Supervised Learning", "technique", "یادگیری بانظارت|یادگیری با نظارت"),
    ("Unsupervised Learning", "technique", "یادگیری بدون نظارت"),
    ("Federated Learning", "technique", "یادگیری فدرال"),
    ("Generative AI", "concept", "هوش مصنوعی مولد|genai|gen ai|جنراتیو ای‌آی"),
    ("Computer Vision Models", "concept", "مدل‌های بینایی"),
    ("Object Detection", "technique", "تشخیص شیء|تشخیص اشیا|object detection"),
    ("Image Segmentation", "technique", "بخش‌بندی تصویر|سگمنتیشن|segmentation"),
    ("OCR", "technique", "او سی آر|متن‌خوان|متن خوان"),
    ("Speech-to-Text", "technique", "تبدیل گفتار به متن|stt|asr|تشخیص گفتار"),
    ("Text-to-Speech", "technique", "تبدیل متن به گفتار|tts|گفتارساز"),
    ("Embeddings", "technique", "امبدینگ|امبدینگ‌ها|embedding| برداری‌سازی"),
    ("Vector Database", "tool", "دیتابیس برداری|پایگاه داده برداری|vector db"),
    ("MLOps", "practice", "ام‌ال‌اپس|m l ops"),
    ("DevOps", "practice", "دواپس|dev ops"),
    ("Data Engineering", "concept", "مهندسی داده"),
    ("Data Analysis", "concept", "تحلیل داده|دیتا آنالیز|data analytics"),
    ("Data Science", "concept", "علم داده|دیتا ساینس"),
    ("Data Mining", "concept", "داده کاوی|داده‌کاوی"),
    ("Statistics", "concept", "آمار"),
    ("A/B Testing", "technique", "تست a/b|آزمون ab"),
    ("CI/CD", "practice", "سی‌آی/سی‌دی|ci cd"),
    ("REST API", "concept", "restful api|وب سرویس|rest apis|restful"),
    ("GraphQL", "language", "گراف‌کیو‌ال"),
    ("Web Scraping", "technique", "اسکرپینگ|کراولینگ|web crawling|خزش وب"),
    ("Data Visualization", "concept", "مصورسازی داده|دیتا ویژوالیزیشن"),
    ("Time Series Analysis", "concept", "تحلیل سری زمانی|سری زمانی"),
    ("Recommender Systems", "concept", "سیستم پیشنهاددهنده|recommendation systems"),
    ("Transformer", "concept", "ترنسفورمر|transformers"),
    ("Attention Mechanism", "concept", "مکانیزم توجه|attention"),
    ("Transfer Learning", "technique", "یادگیری انتقالی|transfer learning"),
    ("Model Deployment", "practice", "استقرار مدل|model serving"),
    ("Model Evaluation", "practice", "ارزیابی مدل|model evaluation"),
    ("Data Curation", "practice", "داده‌سازی|دیتاست‌سازی|data curation"),
    ("Post-training", "technique", "پست‌ترینینگ|پست ترینینگ"),
    ("SFT", "technique", "es‌اف‌تی|sft|تنظیم دقیق نظارت‌شده"),
    ("DPO", "technique", "دی‌پی‌او"),
    ("RLHF", "technique", "آر‌ال‌اچ‌اف"),
    ("Reward Modeling", "technique", "مدل‌سازی پاداش"),
    ("Synthetic Data Generation", "technique", "داده مصنوعی"),
    ("Multimodal Models", "concept", "مدل‌های چندوجهی|multimodal"),
    ("Function Calling", "technique", "فراخوانی تابع|function calling"),
    ("Communication", "soft_skill", "ارتباطات|مهارت ارتباطی|ارتباط موثر"),
    ("Teamwork", "soft_skill", "کار تیمی|کار گروهی"),
    ("Problem Solving", "soft_skill", "حل مسئله|حل‌مسئله"),
    ("Time Management", "soft_skill", "مدیریت زمان"),
    ("Self-learning", "soft_skill", "یادگیری مستقل|خودآموزی"),
    ("English Proficiency", "soft_skill", "زبان انگلیسی|انگلیسی"),
])

rows("role", [
    ("AI Engineer", "", "مهندس هوش مصنوعی|ai developer"),
    ("Machine Learning Engineer", "", "مهندس یادگیری ماشین|ml engineer"),
    ("Data Scientist", "", "دانشمند داده|دیتا ساینتیست"),
    ("Data Analyst", "", "تحلیلگر داده|دیتا آنالیست"),
    ("Data Engineer", "", "مهندس داده|دیتا انجینیر"),
    ("AI Specialist", "", "متخصص هوش مصنوعی"),
    ("AI Research Engineer", "", "مهندس پژوهش هوش مصنوعی"),
    ("Computer Vision Engineer", "", "مهندس بینایی ماشین"),
    ("NLP Engineer", "", "مهندس پردازش زبان طبیعی"),
    ("LLM Engineer", "", "مهندس ال‌ال‌ام"),
    ("MLOps Engineer", "", "مهندس ام‌ال‌اپس"),
    ("Prompt Engineer", "", "مهندس پرامپت"),
    ("AI Product Manager", "", "مدیر محصول هوش مصنوعی"),
    ("BI Analyst", "", "تحلیلگر بی‌آی|business intelligence analyst"),
    ("AI Instructor", "", "مدرس هوش مصنوعی"),
    ("Speech AI Engineer", "", "مهندس گفتار"),
    ("Deep Learning Engineer", "", "مهندس یادگیری عمیق"),
    ("Backend Developer", "", "برنامه‌نویس بک‌اند|بک اند دولوپر"),
    ("Software Engineer", "", "مهندس نرم‌افزار|برنامه‌نویس"),
])

rows("domain", [
    ("AI/ML", "", "هوش مصنوعی و یادگیری ماشین|ai ml|ai/ml"),
    ("Data Science", "", "علم داده"),
    ("Data Engineering", "", "مهندسی داده"),
    ("Data Analytics", "", "تحلیل داده"),
    ("Business Intelligence", "", "هوش تجاری|bi"),
    ("Software Engineering", "", "مهندسی نرم‌افزار|نرم‌افزار"),
    ("Project Management", "", "مدیریت پروژه"),
])

rows("seniority", [
    ("junior", "", "جونیور|مبتدی|تازه‌کار|junior level"),
    ("mid", "", "میان‌رده|متوسط|mid level|mid-level"),
    ("senior", "", "سنیور|ارشد|با تجربه|senior level"),
    ("lead", "", "لید|سرپرست|team lead"),
    ("principal", "", "پرینسیپال"),
    ("staff", "", "استف"),
    ("head", "", "مدیر|head of"),
    ("unknown", "", "نامشخص|unknown"),
])

rows("organization", [
    # Placeholder: real organization names will be provided by the user.
])

rows("industry", [
    ("E-commerce", "", "تجارت الکترونیک|فروشگاه اینترنتی|ای‌کامرس"),
    ("Fintech", "", "فین‌تک|مالی"),
    ("Healthcare", "", "سلامت|درمان|پزشکی"),
    ("Education", "", "آموزش|edu‌tech"),
    ("Consulting", "", "مشاوره"),
    ("Software", "", "نرم‌افزار"),
    ("Advertising", "", "تبلیغات"),
    ("Gaming", "", "بازی|گیم"),
    ("Telecommunications", "", "مخابرات|ارتباطات"),
    ("Automotive", "", "خودرو|خودرویی"),
])

rows("compensation_type", [
    ("salary", "", "حقوق|دستمزد|salary"),
    ("hourly", "", "ساعتی|per hour|hourly rate"),
    ("project_based", "", "پروژه‌ای|per project"),
    ("equity", "", "سهام|equity|دان‌سهم"),
    ("commission", "", "پورسانت|commission"),
    ("internship_stipend", "", "کمک هزینه کارآموزی|حق کارآموزی"),
])

rows("benefit", [
    ("Insurance", "", "بیمه|بیمهٔ تکمیلی|بیمه تکمیلی"),
    ("Bonus", "", "پاداش|bonus"),
    ("Stock Options", "", "سهام|سهام شرکت"),
    ("Remote Work", "", "دورکاری|دور کاری"),
    ("Flexible Hours", "", "ساعت کاری منعطف|ساعت کاری انعطاف‌پذیر|ساعت منعطف"),
    ("Training Budget", "", "بودجه آموزش|دوره‌های آموزشی"),
    ("Meal", "", "نهار|وعده غذایی|سرویس غذ"),
    ("Transportation", "", "سرویس ایاب و ذهاب|سرویس رفت و آمد|سرویس"),
    ("Gym", "", "باشگاه|باشگاه ورزشی"),
    ("Relocation Assistance", "", "کمک هزینه اسکان|اسکان"),
    ("MacBook", "", "لپ تاپ|لپ‌تاپ|laptop"),
    ("Paid Time Off", "", "مرخصی|مرخصی با حقوق"),
])

rows("education", [
    ("Bachelor's degree", "", "کارشناسی|لیسانس|bachelor"),
    ("Master's degree", "", "کارشناسی ارشد|ارشد|فوق لیسانس|master"),
    ("PhD", "", "دکتری|دکترا|phd|doctorate"),
    ("Associate degree", "", "فوق دیپلوم|کاردانی|associate"),
    ("Diploma", "", "دیپلم|diploma|سیکل"),
    ("Postdoc", "", "فوق دکتری|postdoc"),
])

rows("education_field", [
    ("Computer Engineering", "", "مهندسی کامپیوتر"),
    ("Computer Science", "", "علوم کامپیوتر"),
    ("Information Technology", "", "فناوری اطلاعات|آی تی|it"),
    ("Electrical Engineering", "", "مهندسی برق"),
    ("Artificial Intelligence", "", "هوش مصنوعی"),
    ("Software Engineering", "", "مهندسی نرم‌افزار"),
    ("Mathematics", "", "ریاضی|ریاضیات"),
    ("Physics", "", "فیزیک"),
    ("Industrial Engineering", "", "مهندسی صنایع"),
    ("Data Science", "", "علم داده"),
])

rows("language", [
    ("fa", "", "فارسی|persian|farsi"),
    ("en", "", "انگلیسی|english"),
    ("tr", "", "ترکی|turkish|ترکی استانبولی"),
    ("ar", "", "عربی|arabic"),
    ("de", "", "آلمانی|german"),
    ("fr", "", "فرانسه|فرانسوی|french"),
])

rows("location", [
    ("تهران", "city", "tehran|طهران"),
    ("مشهد", "city", "mashhad"),
    ("اصفهان", "city", "isfahan|esfahan"),
    ("شیراز", "city", "shiraz"),
    ("تبریز", "city", "tabriz"),
    ("کرج", "city", "karaj"),
    ("اهواز", "city", "ahvaz|ahwaz"),
    ("قم", "city", "qom"),
    ("یزد", "city", "yazd"),
    ("رشت", "city", "rasht"),
    ("ساری", "city", "sari"),
    ("پردیس", "city", "pardis"),
    ("بومهن", "city", "bumahen"),
    ("خرمشهر", "city", "khorramshahr"),
    ("ژاپن", "country", "japan"),
    ("دورکاری", "remote", "remote|کاملاً دورکار|فول ریموت"),
])

rows("employment_type", [
    ("full_time", "", "تمام وقت|تمام‌وقت|فول تایم|نیمه اول|all time|full time|تمام‌وقت"),
    ("part_time", "", "پاره وقت|پارت تایم|نیمه وقت|part time"),
    ("contract", "", "قراردادی|قرارداد|contract|پیمانی"),
    ("internship", "", "کارآموزی|اینترنشیپ|intern|کاراموزی"),
    ("project_based", "", "پروژه‌ای|project based|outsource"),
])

rows("work_mode", [
    ("onsite", "", "حضوری|آفلاین|onsite|in person"),
    ("remote", "", "دورکاری|ریموت|دور کار|remote|fully remote"),
    ("hybrid", "", "هیبرید|ترکیبی|حضوری و دورکاری|hybrid|دورکاری و حضوری"),
])

rows("education_degree", [
    ("any", "", "مهم نیست|فرقی ندارد|هر مقطع|بدون محدودیت تحصیلی|any degree"),
    ("secondary", "", "دیپلم|زیر دیپلم|سیکل اول|high school"),
    ("post_secondary", "", "فوق دیپلم؟|پیش دانشگاهی"),
    ("associate", "", "فوق دیپلم|کاردانی|associate"),
    ("bachelor", "", "کارشناسی|لیسانس|bachelor|bsc|bs"),
    ("master", "", "کارشناسی ارشد|ارشد|فوق لیسانس|master|msc|ms"),
    ("doctorate", "", "دکتری|دکترا|phd"),
    ("professional_doctorate", "", "دکتری تخصصی|دکترای حرفه‌ای"),
])

rows("military_service_requirement", [
    ("any", "", "مهم نیست|فرقی ندارد|بدون محدودیت|any"),
    ("completed_or_exempt", "", "پایان خدمت یا معاف|انجام شده یا معافیت|completed or exempt"),
    ("completed_only", "", "پایان خدمت|کارتمام|کارت پایان خدمت|completed"),
    ("exempt_only", "", "معافیت|معاف|exempt|معافیت دائم"),
    ("deferment_accepted", "", "اعزام|اعسار؟|معافیت تحصیلی|deferment|مجاز به اعزام"),
    ("pending_accepted", "", "در حال خدمت|در حال انجام خدمت|مشمول"),
    ("amriyeh_offered", "", "امریه|amriyeh"),
])

rows("gender_requirement", [
    ("any", "", "مهم نیست|فرقی ندارد|بدون محدودیت|آقا و خانم|any"),
    ("male", "", "آقا|مرد|آقایان|male|جنسیت آقا"),
    ("female", "", "خانم|زن|بانو|female|بانوان"),
])

print("done")
