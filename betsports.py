import os
import re
from datetime import datetime, timezone
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecreto123'
# Puxa o link do Neon na nuvem; se não achar, usa o SQLite local
db_url = os.environ.get('DATABASE_URL', 'postgresql://neondb_owner:npg_meVRBsiA3D2U@ep-gentle-salad-adp0i6vu-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require')

# Correção necessária: o SQLAlchemy exige que comece com 'postgresql://', 
# mas algumas nuvens entregam como 'postgres://'
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

OPCOES_PADRAO = [
    "Casa vence", "Empate", "Fora vence",
    "+ de 0 gol","+ 1 gol", "+ 2 gols", "+ 3 gols", "+ 4 gols", "+ 5 gols",
    "- 1 gol", "- 2 gols", "- 3 gols", "- 4 gols", "- 5 gols",
    "gol de cabeça", "sem gols", "+ 2 cartões", "expulsões","gol de bicicleta","+ 5 escanteios","+ 10 escanteios","- 10 escanteios","- 5 escanteios","gol de penalti","penalti perdido","gol no primeiro tempo","sem gol primeiro tempo","+ 0 gol no primeiro tempo","+ de 1 gol no primeiro tempo","ambos marcam - sim ","ambos marcam - nao"]

# ================= MODELOS DE BANCO DE DADOS =================

bet_odds = db.Table('bet_odds',
    db.Column('bet_id', db.Integer, db.ForeignKey('bet.id'), primary_key=True),
    db.Column('odd_id', db.Integer, db.ForeignKey('odd.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    balance = db.Column(db.Float, default=0.0)
    bets = db.relationship('Bet', backref='user', lazy=True)
    transactions = db.relationship('Transaction', backref='user', lazy=True)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Aberta')  # Aberta, Ao Vivo, Trancada, Finalizado
    current_progress = db.Column(db.Integer, default=0)  # Quantidade de gols live
    home_score = db.Column(db.Integer, default=0)        # Gols do time da Casa
    away_score = db.Column(db.Integer, default=0) 
    home_headers = db.Column(db.Integer, default=0)  # Gols de cabeça - Casa
    away_headers = db.Column(db.Integer, default=0)  # Gols de cabeça - Fora
    home_cards = db.Column(db.Integer, default=0)     # Cartões Amarelos - Casa
    away_cards = db.Column(db.Integer, default=0)     # Cartões Amarelos - Fora
    
    # --- CAMPOS ADICIONADOS PARA OS ESCUDOS E NOMES SEPARADOS (OPÇÃO A) ---
    home_team = db.Column(db.String(100), nullable=False, default="Time Casa")
    away_team = db.Column(db.String(100), nullable=False, default="Time Fora")
    home_logo = db.Column(db.String(500), nullable=True)  # URL ou link do escudo mandante
    away_logo = db.Column(db.String(500), nullable=True)  # URL ou link do escudo visitante

    # --- CAMPOS DE EXPULSÕES (Resolve o bug do "x" vermelho que ficava vazio) ---
    home_expulsions = db.Column(db.Integer, default=0)
    away_expulsions = db.Column(db.Integer, default=0)
    
    # --- NOVOS CAMPOS ADICIONADOS PARA SUPORTAR AS NOVAS ODDS ---
    home_corners = db.Column(db.Integer, default=0)
    away_corners = db.Column(db.Integer, default=0)
    home_bicycle_goals = db.Column(db.Integer, default=0)
    away_bicycle_goals = db.Column(db.Integer, default=0)
    home_penalties_scored = db.Column(db.Integer, default=0)
    away_penalties_scored = db.Column(db.Integer, default=0)
    home_penalties_missed = db.Column(db.Integer, default=0)
    away_penalties_missed = db.Column(db.Integer, default=0)
    home_first_half_goals = db.Column(db.Integer, default=0)
    away_first_half_goals = db.Column(db.Integer, default=0)
    
    # --- SISTEMA DE RELÓGIO E TEMPO DE JOGO ---
    period = db.Column(db.String(50), default="Não Iniciado") 
    timer_active = db.Column(db.Boolean, default=False)
    timer_start_time = db.Column(db.DateTime, nullable=True)
    saved_seconds = db.Column(db.Integer, default=0)
    
    odds = db.relationship('Odd', backref='game', lazy=True)

    def get_current_time(self):
        """Retorna os minutos e segundos atuais calculados no servidor de forma dinâmica"""
        if not self.timer_active or not self.timer_start_time:
            total_seconds = self.saved_seconds
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            start = self.timer_start_time.replace(tzinfo=None)
            elapsed = (now - start).total_seconds()
            total_seconds = self.saved_seconds + int(elapsed)
        return total_seconds // 60, total_seconds % 60

class Odd(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    description = db.Column(db.String(100), nullable=False)
    multiplier = db.Column(db.Float, nullable=False)
    is_winner = db.Column(db.Boolean, default=False)

    def is_currently_hitting(self):
        """Avalia se este palpite específico está se concretizando no momento atual da partida"""
        if self.game.status == 'Finalizado':
            return self.is_winner
            
        home = self.game.home_score or 0
        away = self.game.away_score or 0
        total_goals = home + away
        total_cards = (self.game.home_cards or 0) + (self.game.away_cards or 0)
        total_headers = (self.game.home_headers or 0) + (self.game.away_headers or 0)
        
        # Novas métricas integradas para checagem ao vivo
        total_corners = (self.game.home_corners or 0) + (self.game.away_corners or 0)
        total_bicycle = (self.game.home_bicycle_goals or 0) + (self.game.away_bicycle_goals or 0)
        total_penalties_scored = (self.game.home_penalties_scored or 0) + (self.game.away_penalties_scored or 0)
        total_penalties_missed = (self.game.home_penalties_missed or 0) + (self.game.away_penalties_missed or 0)
        total_first_half_goals = (self.game.home_first_half_goals or 0) + (self.game.away_first_half_goals or 0)
        
        desc = self.description.lower().strip()
        
        if "casa vence" in desc:
            return home > away
        elif "fora vence" in desc:
            return away > home
        elif "empate" in desc:
            return home == away
        elif "sem gols" in desc:
            return total_goals == 0
        elif "sem gol primeiro tempo" in desc:
            return total_first_half_goals == 0
        elif "gol de bicicleta" in desc:
            return total_bicycle > 0
        elif "gol de penalti" in desc or "gol de pênalti" in desc:
            return total_penalties_scored > 0
        elif "penalti perdido" in desc or "pênalti perdido" in desc:
            return total_penalties_missed > 0
        elif "gol de cabeça" in desc and not any(x in desc for x in ["+", "-", "mais de", "menos de"]):
            return total_headers > 0
        elif "gol no primeiro tempo" in desc and not any(x in desc for x in ["+", "-", "mais de", "menos de"]):
            return total_first_half_goals > 0
            
        # Tratamento genérico expandido para linhas de mais/menos com números (+ 5 escanteios, etc)
        nums = re.findall(r'\d+(?:\.\d+)?', desc)
        if nums:
            val = float(nums[0])
            if "cart" in desc or "card" in desc:
                metric = total_cards
            elif "cabeç" in desc or "header" in desc:
                metric = total_headers
            elif "escanteio" in desc or "corner" in desc:
                metric = total_corners
            elif "primeiro tempo" in desc or "1º tempo" in desc:
                metric = total_first_half_goals
            else:
                metric = total_goals
                
            if "+" in desc or "mais de" in desc or "over" in desc:
                return metric > val
            elif "-" in desc or "menos de" in desc or "under" in desc:
                return metric <= val
                
        return False

class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    total_multiplier = db.Column(db.Float, nullable=False)
    potential_win = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Pendente')  # Pendente, Ganhou, Perdeu, Cashout
    manual_cashout_value = db.Column(db.Float, nullable=True, default=None)
    odds = db.relationship('Odd', secondary=bet_odds, backref=db.backref('bets', lazy=True))

    def calculate_live_cashout(self):
        """Calcula o Cash Out dinâmico baseado no progresso real dos jogos"""
        if self.manual_cashout_value is not None:
            return round(self.manual_cashout_value, 2)

        if self.status != 'Pendente':
            return 0.0
        
        total_weight = 1.0
        
        for odd in self.odds:
            game = odd.game
            if game.status == 'Trancada':
                total_weight *= 0.1
                continue
            elif game.status == 'Finalizado':
                total_weight *= 0.5
                continue
            
            home = game.home_score or 0
            away = game.away_score or 0
            
            if "Casa vence" in odd.description:
                if home > away:
                    vantagem = home - away
                    total_weight *= (1.0 + (vantagem * 0.25))
                elif home == away:
                    total_weight *= 0.70
                else:
                    desvantagem = away - home
                    total_weight *= max(0.05, 0.35 - (desvantagem * 0.15))

            elif "Fora vence" in odd.description:
                if away > home:
                    vantagem = away - home
                    total_weight *= (1.0 + (vantagem * 0.25))
                elif home == away:
                    total_weight *= 0.70
                else:
                    desvantagem = home - away
                    total_weight *= max(0.05, 0.35 - (desvantagem * 0.15))

            elif "Empate" in odd.description:
                if home == away:
                    total_weight *= 1.10
                else:
                    distancia = abs(home - away)
                    total_weight *= max(0.05, 0.40 - (distancia * 0.20))
                    
            else:
                numbers = re.findall(r'\d+', odd.description)
                target = int(numbers[0]) if numbers else 1
                current = game.current_progress or 0
                
                if "+" in odd.description:
                    if current > target:
                        total_weight *= odd.multiplier
                    else:
                        proximity = current / (target + 1) if target >= 0 else 0
                        partial_multiplier = 1.0 + (odd.multiplier - 1.0) * proximity * 0.65
                        total_weight *= partial_multiplier
                else:
                    if current <= target:
                        total_weight *= odd.multiplier
                    else:
                        total_weight *= 0.05
                
        cashout_value = self.amount * total_weight * 0.80
        if cashout_value > self.potential_win:
            cashout_value = self.potential_win * 0.85
            
        return round(cashout_value, 2)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), default='Pendente')
    date = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ================= FUNÇÃO DE LIQUIDAÇÃO ANTECIPADA AUTOMÁTICA =================

def check_and_settle_live_bets(game):
    """
    Varre os bilhetes pendentes sempre que há uma atualização para verificar se 
    mercados já ganharam e realiza o pagamento imediato.
    """
    pending_bets = Bet.query.filter_by(status='Pendente').all()
    for bet in pending_bets:
        if not any(o.game_id == game.id for o in bet.odds):
            continue

        all_resolved_and_won = True
        for o in bet.odds:
            if o.game.status != 'Finalizado' and not o.is_winner:
                all_resolved_and_won = False
                break
            elif o.game.status == 'Finalizado' and not o.is_winner:
                bet.status = 'Perdeu'
                all_resolved_and_won = False
                break

        if all_resolved_and_won:
            bet.status = 'Ganhou'
            bet.user.balance += bet.potential_win

    db.session.commit()

# ================= ROTAS DE AUTENTICAÇÃO E CONTA =================

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('admin_dashboard' if user.is_admin else 'dashboard'))
        flash('Usuário ou senha inválidos.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('Este usuário já existe.')
            return redirect(url_for('register'))
        
        is_admin = False
        if User.query.count() == 0:
            is_admin = True
            
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password, is_admin=is_admin, balance=0.0)
        db.session.add(new_user)
        db.session.commit()
        flash('Cadastro realizado com sucesso! Faça login.')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/finance', methods=['GET', 'POST'])
@login_required
def finance():
    if request.method == 'POST':
        amount = float(request.form.get('amount', 0))
        # Captura o valor exato que vem do seu <select name="type">
        tx_type = request.form.get('type') 
        
        if amount > 0 and tx_type in ['Deposito', 'Saque']:
            new_tx = Transaction(
                user_id=current_user.id, 
                amount=amount, 
                type=tx_type, # Salva como 'Deposito' ou 'Saque'
                status='Pendente'
            )
            db.session.add(new_tx)
            db.session.commit()
            flash(f'Solicitação de {tx_type} enviada com sucesso!')
        else:
            flash('Erro: Valor inválido ou tipo de transação não selecionado.')
            
    txs = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.date.desc()).all()
    return render_template('finance.html', transactions=txs)

# ================= PAINEL DO USUÁRIO & CASHOUT =================

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        odd_ids = request.form.getlist('odds')
        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            amount = 0.0

        if amount <= 0 or not odd_ids:
            flash('Selecione ao menos um palpite e insira um valor válido.')
            return redirect(url_for('dashboard'))
            
        if current_user.balance < amount:
            flash('Saldo insuficiente!')
            return redirect(url_for('dashboard'))

        total_multiplier = 1.0
        odds_objects = []
        for o_id in odd_ids:
            odd_obj = db.session.get(Odd, int(o_id))
            if odd_obj:
                if odd_obj.game.status in ['Trancada', 'Finalizado']:
                    flash(f'O mercado para o jogo "{odd_obj.game.title}" está fechado/suspenso.')
                    return redirect(url_for('dashboard'))
                odds_objects.append(odd_obj)
                total_multiplier *= odd_obj.multiplier
                
        current_user.balance -= amount
        new_bet = Bet(
            user_id=current_user.id,
            amount=amount,
            total_multiplier=round(total_multiplier, 2),
            potential_win=round(amount * total_multiplier, 2),
            status='Pendente'
        )
        new_bet.odds.extend(odds_objects)
        db.session.add(new_bet)
        db.session.commit()
        flash('Bilhete registrado com sucesso!')
        return redirect(url_for('bet_history'))

    games = Game.query.filter(Game.status != 'Finalizado').all()
    return render_template('dashboard.html', games=games)

@app.route('/bet_history')
@login_required
def bet_history():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.id.desc()).all()
    return render_template('bet_history.html', bets=bets)

@app.route('/bet/cashout/<int:bet_id>', methods=['POST'])
@login_required
def cashout(bet_id):
    bet = db.session.get(Bet, bet_id)
    if not bet or bet.user_id != current_user.id or bet.status != 'Pendente':
        flash('Não foi possível processar o cash out.')
        return redirect(url_for('bet_history'))
    
    for odd in bet.odds:
        if odd.game.status == 'Trancada':
            flash('Cash Out indisponível no momento: Mercados suspensos.')
            return redirect(url_for('bet_history'))
        if odd.game.status == 'Finalizado':
            flash('A partida já terminou. Aguarde a liquidação.')
            return redirect(url_for('bet_history'))
            
    cashout_value = bet.calculate_live_cashout()

    current_user.balance += cashout_value
    bet.status = 'Cashout'
    db.session.commit()
    flash(f'Cash Out realizado! R$ {cashout_value:.2f} foram adicionados à sua conta.')
    return redirect(url_for('bet_history'))

# ================= PAINEL ADMINISTRATIVO & LIQUIDAÇÃO =================

@app.route('/admin/dashboard', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    # Verifica se é admin
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    
    # --- LÓGICA DE POST (CRIAÇÃO DE PARTIDA) ---
    if request.method == 'POST':
        title_input = request.form.get('title') 
        if title_input:
            if " x " in title_input.lower():
                teams = title_input.split(' x ')
            elif " vs " in title_input.lower():
                teams = title_input.split(' vs ')
            else:
                teams = [title_input, "Time Visitante"]
            
            home_team = teams[0].strip()
            away_team = teams[1].strip() if len(teams) > 1 else "Time Visitante"
            
            logos_database = {
                "corinthians": "https://cdn.freebiesupply.com/logos/large/2x/esporte-clube-corinthians-de-andradina-sp-logo-png-transparent.png",
                "sao paulo": "https://logodetimes.com/times/sao-paulo/logo-sao-paulo-4096.png",
                "palmeiras": "https://static.wikia.nocookie.net/cftu/images/c/cd/Palmeiras.png/revision/latest/thumbnail/width/360/height/450?cb=20170102174540&path-prefix=pt-br",
                "santos": "https://upload.wikimedia.org/wikipedia/commons/1/15/Santos_Logo.png",
                "flamengo": "https://logodetimes.com/times/flamengo/logo-flamengo-1536.png",
                "real madrid": "https://upload.wikimedia.org/wikipedia/ar/thumb/5/56/Real_Madrid_CF.svg/330px-Real_Madrid_CF.svg.png"
            }
            
            home_logo = logos_database.get(home_team.lower(), "/static/img/default.png")
            away_logo = logos_database.get(away_team.lower(), "/static/img/default.png")
            
            novo_jogo = Game(
                title=title_input,
                home_team=home_team,
                away_team=away_team,
                home_logo=home_logo,
                away_logo=away_logo,
                status='Aberta'
            )
            
            db.session.add(novo_jogo)
            db.session.commit()
            
            for opcao in OPCOES_PADRAO:
                nova_odd = Odd(game_id=novo_jogo.id, description=opcao, multiplier=2.00)
                db.session.add(nova_odd)
            
            db.session.commit()
            flash("Partida e mercados criados com sucesso!")
            return redirect(url_for('admin_dashboard'))

    # --- LÓGICA DE GET (EXIBIÇÃO DOS DADOS) ---
    games = Game.query.order_by(Game.id.desc()).all()
    all_bets = Bet.query.order_by(Bet.id.desc()).all()
    
    # --- TESTE DE DIAGNÓSTICO ---
    # Vamos buscar apenas pelo tipo, sem filtrar pelo status, 
    # e sem complicar com ilike no status por enquanto.
    
    pending_deposits = Transaction.query.filter(Transaction.type.ilike('%deposito%')).all()
    pending_withdrawals = Transaction.query.filter(Transaction.type.ilike('%saque%')).all()

    # LOG NO TERMINAL PARA DEBUGAR
    print(f"DEBUG: Encontrei {len(pending_deposits)} depósitos no banco.")
    for d in pending_deposits:
        print(f"DEBUG: Depósito encontrado -> Tipo: '{d.type}', Status: '{d.status}'")
    
    # Junta tudo em uma única lista que o seu HTML já sabe como usar
    todas_pendentes = Transaction.query.filter(Transaction.status == 'Pendente').all()
    
    return render_template(
        'admin_dashboard.html', 
        games=games, 
        all_bets=all_bets, 
        pending_transactions=todas_pendentes  # Mudei o nome aqui para bater com o HTML
    )
    
    # Se você ainda não vir nada, verifique no seu DB Browser se o tipo está 
    # exatamente 'deposito' (minúsculo) ou 'Deposito' (maiúsculo).
    
    return render_template(
        'admin_dashboard.html', 
        games=games, 
        all_bets=all_bets, 
        deposits=pending_deposits, 
        withdrawals=pending_withdrawals
    )

    # --- RENDERIZAÇÃO NORMAL DA PÁGINA (GET) ---
    games = Game.query.order_by(Game.id.desc()).all()
    pending_transactions = Transaction.query.filter_by(status='Pendente').all()
    all_bets = Bet.query.order_by(Bet.id.desc()).all()
    
    # 🌟 LINHAS ADICIONADAS SEM ALTERAR AS ANTERIORES: Separando depósitos e saques pendentes
    pending_deposits = Transaction.query.filter_by(type='deposit', status='Pendente').all()
    pending_withdrawals = Transaction.query.filter_by(type='withdraw', status='Pendente').all()
    
    # Retornando o template com as suas variáveis originais intactas + as novas tabelas separadas
    return render_template('admin_dashboard.html', games=games, pending_transactions=pending_transactions, all_bets=all_bets, deposits=pending_deposits, withdrawals=pending_withdrawals)

@app.route('/admin/update_game_progress/<int:game_id>', methods=['POST'])
@login_required
def update_game_progress(game_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    game.current_progress = int(request.form.get('current_progress', 0))
    db.session.commit()
    
    check_and_settle_live_bets(game)
    
    flash(f'Progresso do jogo "{game.title}" atualizado!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_game_score/<int:game_id>', methods=['POST'])
@login_required
def update_game_score(game_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    
    # Capturando dados do formulário HTML (Originais e Novos Injetados)
    game.home_score = int(request.form.get('home_score', 0))
    game.away_score = int(request.form.get('away_score', 0))
    game.home_headers = int(request.form.get('home_headers', 0))
    game.away_headers = int(request.form.get('away_headers', 0))
    game.home_cards = int(request.form.get('home_cards', 0))
    game.away_cards = int(request.form.get('away_cards', 0))
    
    game.home_corners = int(request.form.get('home_corners', 0))
    game.away_corners = int(request.form.get('away_corners', 0))
    game.home_bicycle_goals = int(request.form.get('home_bicycle_goals', 0))
    game.away_bicycle_goals = int(request.form.get('away_bicycle_goals', 0))
    game.home_penalties_scored = int(request.form.get('home_penalties_scored', 0))
    game.away_penalties_scored = int(request.form.get('away_penalties_scored', 0))
    game.home_penalties_missed = int(request.form.get('home_penalties_missed', 0))
    game.away_penalties_missed = int(request.form.get('away_penalties_missed', 0))
    game.home_first_half_goals = int(request.form.get('home_first_half_goals', 0))
    game.away_first_half_goals = int(request.form.get('away_first_half_goals', 0))
    
    game.current_progress = game.home_score + game.away_score
    
    home = game.home_score
    away = game.away_score
    total_goals = game.current_progress
    total_cards = game.home_cards + game.away_cards
    total_headers = game.home_headers + game.away_headers
    total_corners = game.home_corners + game.away_corners
    total_bicycle = game.home_bicycle_goals + game.away_bicycle_goals
    total_penalties_scored = game.home_penalties_scored + game.away_penalties_scored
    total_penalties_missed = game.home_penalties_missed + game.away_penalties_missed
    total_first_half_goals = game.home_first_half_goals + game.away_first_half_goals
    
    print(f"\n--- 🔄 ATUALIZANDO JOGO: {game.title} ---")
    print(f"Gols: {total_goals} | Cartões: {total_cards} | Cabeça: {total_headers} | Escanteios: {total_corners}")
    
    # ==========================================================
    # 🤖 SISTEMA DE CHECAGEM ULTRA-ROBUSTO DE ODDS COMPLETO
    # ==========================================================
    for odd in game.odds:
        desc = odd.description.lower().strip()
        print(f" -> Analisando Odd Live: '{odd.description}'")
        
        if "casa vence" in desc:
            odd.is_winner = home > away
        elif "fora vence" in desc:
            odd.is_winner = away > home
        elif "empate" in desc:
            odd.is_winner = home == away
        elif "sem gols" in desc:
            odd.is_winner = total_goals == 0
        elif "sem gol primeiro tempo" in desc:
            odd.is_winner = total_first_half_goals == 0
        elif "gol de bicicleta" in desc:
            odd.is_winner = total_bicycle > 0
        elif "gol de penalti" in desc or "gol de pênalti" in desc:
            odd.is_winner = total_penalties_scored > 0
        elif "penalti perdido" in desc or "pênalti perdido" in desc:
            odd.is_winner = total_penalties_missed > 0
        elif "gol de cabeça" in desc and not any(x in desc for x in ["+", "-", "mais de", "menos de"]):
            odd.is_winner = total_headers > 0
        elif "gol no primeiro tempo" in desc and not any(x in desc for x in ["+", "-", "mais de", "menos de"]):
            odd.is_winner = total_first_half_goals > 0
        else:
            nums = re.findall(r'\d+(?:\.\d+)?', desc)
            if nums:
                val = float(nums[0])
                if "cart" in desc or "card" in desc:
                    metric = total_cards
                elif "cabeç" in desc or "header" in desc:
                    metric = total_headers
                elif "escanteio" in desc or "corner" in desc:
                    metric = total_corners
                elif "primeiro tempo" in desc or "1º tempo" in desc:
                    metric = total_first_half_goals
                else:
                    metric = total_goals
                    
                if "+" in desc or "mais de" in desc or "over" in desc:
                    odd.is_winner = metric > val
                elif "-" in desc or "menos de" in desc or "under" in desc:
                    odd.is_winner = metric <= val
                    
        print(f"    Resultado definido para '{odd.description}': {odd.is_winner}")
    # ==========================================================

    db.session.commit()
    
    # 🔥 Gatilho de checagem imediata para pagar bilhetes ao vivo
    check_and_settle_live_bets(game)
    
    flash(f'Placar de "{game.title}" modificado para {game.home_score}x{game.away_score}!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/force_cashout_value/<int:bet_id>', methods=['POST'])
@login_required
def force_cashout_value(bet_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    bet = db.session.get(Bet, bet_id)
    if bet:
        manual_val = request.form.get('manual_value')
        if manual_val and manual_val.strip() != "":
            bet.manual_cashout_value = float(manual_val)
            flash(f'Cash out do bilhete #{bet.id} travado em R$ {float(manual_val):.2f}!')
        else:
            bet.manual_cashout_value = None
            flash(f'Cash out do bilhete #{bet.id} redefinido para cálculo automático.')
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/create_game', methods=['POST'])
@login_required
def create_game():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
        
    title_input = request.form.get('title')
    initial_multiplier = request.form.get('initial_multiplier', type=float, default=2.00)
    
    if title_input:
        if " x " in title_input.lower():
            teams = title_input.split(' x ')
        elif " vs " in title_input.lower():
            teams = title_input.split(' vs ')
        else:
            teams = [title_input, ""]
        
        home_team = teams[0].strip()
        away_team = teams[1].strip() if len(teams) > 1 else "Visitante"
        
        logos_database = {
                "corinthians": "https://cdn.freebiesupply.com/logos/large/2x/esporte-clube-corinthians-de-andradina-sp-logo-png-transparent.png",
                "sao paulo": "https://logodetimes.com/times/sao-paulo/logo-sao-paulo-4096.png",
                "palmeiras": "https://static.wikia.nocookie.net/cftu/images/c/cd/Palmeiras.png/revision/latest/thumbnail/width/360/height/450?cb=20170102174540&path-prefix=pt-br",
                "santos": "https://upload.wikimedia.org/wikipedia/commons/1/15/Santos_Logo.png",
                "flamengo": "https://logodetimes.com/times/flamengo/logo-flamengo-1536.png",
                "real madrid": "https://upload.wikimedia.org/wikipedia/ar/thumb/5/56/Real_Madrid_CF.svg/330px-Real_Madrid_CF.svg.png"
        }
        
        home_logo = logos_database.get(home_team.lower(), "/static/img/default.png")
        away_logo = logos_database.get(away_team.lower(), "/static/img/default.png")
        
        # CRIAR O JOGO (Sem a linha initial_odds)
        novo_jogo = Game(
            title=title_input,
            home_team=home_team,
            away_team=away_team,
            home_logo=home_logo,
            away_logo=away_logo,
            status='Aberta'
        )
        
        db.session.add(novo_jogo)
        db.session.commit()
        
        # LOOP AUTOMÁTICO: Usa o multiplicador enviado pelo Admin do painel
        for opcao in OPCOES_PADRAO:
            nova_odd = Odd(game_id=novo_jogo.id, description=opcao, multiplier=initial_multiplier)
            db.session.add(nova_odd)
            
        db.session.commit()
        flash("Partida criada com sucesso!")
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_odds/<int:game_id>', methods=['POST'])
@login_required
def update_odds(game_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    game = db.session.get(Game, game_id)
    if game:
        for odd in game.odds:
            new_val = request.form.get(f'odd_val_{odd.id}')
            if new_val: odd.multiplier = round(float(new_val), 2)
        db.session.commit()
        flash('Odds salvas com sucesso!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/change_status/<int:game_id>/<string:new_status>')
@login_required
def change_status(game_id, new_status):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    game = db.session.get(Game, game_id)
    if game and new_status in ['Aberta', 'Ao Vivo', 'Trancada', 'Finalizado']:
        game.status = new_status
        db.session.commit()
        flash(f'Status do jogo alterado para {new_status}!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_game/<int:game_id>', methods=['POST'])
@login_required
def delete_game(game_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    game = db.session.get(Game, game_id)
    if game and game.status == 'Finalizado':
        for odd in game.odds:
            db.session.execute(bet_odds.delete().where(bet_odds.c.odd_id == odd.id))
            db.session.delete(odd)
        db.session.delete(game)
        db.session.commit()
        flash('Jogo finalizado e seu histórico foram excluídos permanentemente!')
    else:
        flash('Erro: Apenas partidas com status "Finalizado" podem ser excluídas.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settle_game/<int:game_id>', methods=['POST'])
@login_required
def settle_game(game_id):
    if not getattr(current_user, 'is_admin', False):
        return "Não autorizado", 403
        
    game = db.session.get(Game, game_id)
    if not game:
        return "Jogo não encontrado", 404
        
    home = game.home_score or 0
    away = game.away_score or 0
    total_goals = home + away
    total_cards = (game.home_cards or 0) + (game.away_cards or 0)
    total_headers = (game.home_headers or 0) + (game.away_headers or 0)
    total_corners = (game.home_corners or 0) + (game.away_corners or 0)
    total_bicycle = (game.home_bicycle_goals or 0) + (game.away_bicycle_goals or 0)
    total_penalties_scored = (game.home_penalties_scored or 0) + (game.away_penalties_scored or 0)
    total_penalties_missed = (game.home_penalties_missed or 0) + (game.away_penalties_missed or 0)
    total_first_half_goals = (game.home_first_half_goals or 0) + (game.away_first_half_goals or 0)

    # ====================================================================
    # STEP 1: CALCULA E GRAVA AUTOMATICAMENTE OS VENCEDORES NO BANCO
    # ====================================================================
    for odd in game.odds:
        desc = odd.description.lower().strip()
        
        if "casa vence" in desc:
            odd.is_winner = home > away
        elif "fora vence" in desc:
            odd.is_winner = away > home
        elif "empate" in desc:
            odd.is_winner = home == away
        elif "sem gols" in desc:
            odd.is_winner = total_goals == 0
        elif "sem gol primeiro tempo" in desc:
            odd.is_winner = total_first_half_goals == 0
        elif "gol de bicicleta" in desc:
            odd.is_winner = total_bicycle > 0
        elif "gol de penalti" in desc or "gol de pênalti" in desc:
            odd.is_winner = total_penalties_scored > 0
        elif "penalti perdido" in desc or "pênalti perdido" in desc:
            odd.is_winner = total_penalties_missed > 0
        elif "gol de cabeça" in desc and not any(x in desc for x in ["+", "-", "mais de", "menos de"]):
            odd.is_winner = total_headers > 0
        elif "gol no primeiro tempo" in desc and not any(x in desc for x in ["+", "-", "mais de", "menos de"]):
            odd.is_winner = total_first_half_goals > 0
        else:
            nums = re.findall(r'\d+(?:\.\d+)?', desc)
            if nums:
                val = float(nums[0])
                if "cart" in desc or "card" in desc:
                    metric = total_cards
                elif "cabeç" in desc or "header" in desc:
                    metric = total_headers
                elif "escanteio" in desc or "corner" in desc:
                    metric = total_corners
                elif "primeiro tempo" in desc or "1º tempo" in desc:
                    metric = total_first_half_goals
                else:
                    metric = total_goals
                    
                if "+" in desc or "mais de" in desc or "over" in desc:
                    odd.is_winner = metric > val
                elif "-" in desc or "menos de" in desc or "under" in desc:
                    odd.is_winner = metric <= val

    game.status = 'Finalizado'
    db.session.commit()
    
    # ====================================================================
    # STEP 2: VALIDAÇÃO DOS BILHETES PENDENTES
    # ====================================================================
    pending_bets = Bet.query.filter_by(status='Pendente').all()
    
    for bet in pending_bets:
        if any(odd.game_id == game.id for odd in bet.odds):
            all_games_finished = True
            ticket_won = True
            
            for odd in bet.odds:
                if odd.game.status != 'Finalizado':
                    all_games_finished = False
                    continue
                
                if not odd.is_winner:
                    ticket_won = False
                    bet.status = 'Perdeu'
                    break
            
                if all_games_finished and ticket_won:
                 bet.status = 'Ganhou'
                if bet.user:
                    if hasattr(bet.user, 'saldo'):
                        bet.user.saldo += bet.potential_win
                    elif hasattr(bet.user, 'balance'):
                        bet.user.balance += bet.potential_win

    db.session.commit()
    
    flash(f"O confronto '{game.title}' foi finalizado com sucesso e os bilhetes foram processados.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/approve_transaction/<int:tx_id>', methods=['POST'])
@login_required
def approve_transaction(tx_id):
    if not current_user.is_admin: 
        return redirect(url_for('dashboard'))
        
    tx = db.session.get(Transaction, tx_id)
    
    if not tx:
        flash("Transação não encontrada.")
        return redirect(url_for('admin_dashboard'))

    if tx.status != 'Pendente':
        flash("Esta transação já foi processada.")
        return redirect(url_for('admin_dashboard'))

    # Normalizamos o tipo para garantir a comparação
    tipo = str(tx.type).strip().capitalize()

    # Lógica de Depósito
    if tipo == 'Deposito':
        tx.user.balance += tx.amount
        tx.status = 'Aprovado'
        flash(f"Depósito de R$ {tx.amount:.2f} aprovado com sucesso!")
            
    # Lógica de Saque
    elif tipo == 'Saque':
        if tx.user.balance >= tx.amount:
            tx.user.balance -= tx.amount
            tx.status = 'Aprovado'
            flash(f"Saque de R$ {tx.amount:.2f} aprovado com sucesso!")
        else:
            flash(f"Erro: Usuário não tem saldo suficiente para este saque!")
            return redirect(url_for('admin_dashboard'))
    
    else:
        flash(f"Erro: Tipo de transação desconhecido ('{tipo}').")
        return redirect(url_for('admin_dashboard'))
                
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/reject_transaction/<int:tx_id>', methods=['POST'])
@login_required
def reject_transaction(tx_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    tx = db.session.get(Transaction, tx_id)
    if tx and tx.status == 'Pendente':
        tx.status = 'Rejeitado'
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle_odd_winner/<int:odd_id>', methods=['POST'])
@login_required
def toggle_odd_winner(odd_id):
    if not getattr(current_user, 'is_admin', False):
        return jsonify({'success': False, 'message': 'Não autorizado'}), 403
        
    odd = db.session.get(Odd, odd_id) 
    if not odd:
        return jsonify({'success': False, 'message': 'Odd não encontrada'}), 404
        
    odd.is_winner = not odd.is_winner
    db.session.commit()
    
    return jsonify({'success': True, 'is_winner': odd.is_winner})

# --- NOVA ROTA ADMINISTRATIVA PARA CONTROLE DO CRONÔMETRO ---
@app.route('/admin/control_timer/<int:game_id>/<string:action>', methods=['POST'])
@login_required
def control_timer(game_id, action):
    if not getattr(current_user, 'is_admin', False):
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    
    if action == 'start':
        if not game.timer_active:
            game.timer_active = True
            game.timer_start_time = datetime.now(timezone.utc).replace(tzinfo=None)
            if game.period == "Não Iniciado":
                game.period = "1º Tempo"
            flash(f'Cronômetro de "{game.title}" iniciado!')
    elif action == 'pause':
        if game.timer_active and game.timer_start_time:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            elapsed = (now - game.timer_start_time).total_seconds()
            game.saved_seconds += int(elapsed)
            game.timer_active = False
            game.timer_start_time = None
            flash(f'Cronômetro de "{game.title}" pausado!')
    elif action == 'reset':
        game.timer_active = False
        game.timer_start_time = None
        game.saved_seconds = 0
        game.period = "Não Iniciado"
        flash(f'Cronômetro de "{game.title}" reiniciado!')
    elif action == 'next_period':
        if game.timer_active and game.timer_start_time:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            elapsed = (now - game.timer_start_time).total_seconds()
            game.saved_seconds += int(elapsed)
            game.timer_start_time = datetime.now(timezone.utc).replace(tzinfo=None)
            
        if game.period == "1º Tempo":
            game.period = "Intervalo"
        elif game.period == "Intervalo":
            game.period = "2º Tempo"
        elif game.period == "2º Tempo":
            game.period = "Fim de Jogo"
        flash(f'Período de "{game.title}" alterado para {game.period}!')
        
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
