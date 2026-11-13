from flask import Blueprint, render_template, jsonify, request, send_file
from CTFd.plugins import register_plugin_assets_directory
from CTFd.utils.decorators import admins_only
from CTFd.models import Users, Challenges, Solves, Teams, Awards, Submissions, db
from CTFd.utils.modes import get_mode_as_word
import csv
import io
import sys
import os
import zipfile
from datetime import datetime

# Nom du dossier du plugin (dynamique -> pas de chemin en dur, portable)
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
_LOGO_URL = '/plugins/%s/images/badgifylogo.png' % _PLUGIN_DIR


# Modèle pour stocker les domaines exclus
class ExcludedDomain(db.Model):
    __tablename__ = 'excluded_domains'
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    def __repr__(self):
        return f'<ExcludedDomain {self.domain}>'


# Modèle pour stocker la configuration utilisateur
class UserConfig(db.Model):
    __tablename__ = 'badges_user_config'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, unique=True)
    config_data = db.Column(db.Text, nullable=False)  # JSON stringifié
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

    def __repr__(self):
        return f'<UserConfig user_id={self.user_id}>'


# ---- Configuration API Badgify (token stocke serveur, jamais renvoye) ----
class BadgifyConfig(db.Model):
    __tablename__ = 'badgify_api_config'
    id = db.Column(db.Integer, primary_key=True)
    api_url = db.Column(db.String(255), nullable=False, default='https://badgify.eu/api/v1/public')
    token = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())


def _badgify_token_hint(token):
    if not token:
        return None
    if len(token) <= 10:
        return token[:2] + '\u2026'
    return token[:5] + '\u2026' + token[-4:]


def _badgify_call(cfg, path, payload=None, timeout=15):
    """Appel serveur -> API Badgify. Retourne (status_code, body_dict)."""
    import urllib.request, urllib.error, json as __j
    url = cfg.api_url.rstrip('/') + path
    data = __j.dumps(payload if payload is not None else {}).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', 'Bearer ' + (cfg.token or ''))
    req.add_header('Content-Type', 'application/json')
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode('utf-8', 'replace')
        try:
            body = __j.loads(raw) if raw else {}
        except Exception:
            body = {'raw': raw}
        return resp.getcode(), body
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            body = __j.loads(raw) if raw else {}
        except Exception:
            body = {'raw': raw}
        return e.code, body
    except Exception as e:
        return 0, {'error': str(e)}


def _badgify_friendly(code):
    return {
        401: "Jeton absent, invalide ou révoqué.",
        403: "Appel depuis une origine non autorisée (IP du serveur non déclarée sur Badgify).",
        404: "Badge inexistant ou non exposé.",
        409: "Quota mensuel d'emails insuffisant.",
        422: "Requête invalide.",
        429: "Limite de débit atteinte, réessayez plus tard.",
        0:   "Impossible de joindre l'API Badgify (réseau / URL).",
    }.get(code)


def load(app):
    """
    Fonction principale de chargement du plugin CTFd
    """
    # Création de la table pour les domaines exclus
    with app.app_context():
        db.create_all()
    
    # Fonction helper pour construire les CSV avec colonnes dynamiques
    def build_csv_row(columns, data_type, data):
        """
        Construit une ligne CSV selon les colonnes sélectionnées
        data_type: 'header', 'user', 'team_member', 'top_user', 'top_team'
        """
        if data_type == 'header_user':
            row = []
            if columns.get('id'): row.append('ID')
            if columns.get('username'): row.append('Username')
            if columns.get('email'): row.append('Email')
            if columns.get('score'): row.append('Score')
            return row
        
        elif data_type == 'header_team':
            row = []
            if columns.get('id'): row.append('Team ID')
            if columns.get('teamname'): row.append('Team Name')
            if columns.get('username'): row.append('Username')
            if columns.get('email'): row.append('Email')
            return row
        
        elif data_type == 'header_top_user':
            row = []
            if columns.get('rank'): row.append('Rank')
            if columns.get('id'): row.append('User ID')
            if columns.get('username'): row.append('Username')
            if columns.get('email'): row.append('Email')
            if columns.get('score'): row.append('Score')
            return row
        
        elif data_type == 'header_top_team':
            row = []
            if columns.get('rank'): row.append('Rank')
            if columns.get('teamname'): row.append('Team Name')
            if columns.get('score'): row.append('Team Score')
            if columns.get('id'): row.append('User ID')
            if columns.get('username'): row.append('Username')
            if columns.get('email'): row.append('Email')
            return row
        
        elif data_type == 'user':
            row = []
            if columns.get('id'): row.append(data['id'])
            if columns.get('username'): row.append(data['name'])
            if columns.get('email'): row.append(data['email'])
            if columns.get('score'): row.append(data.get('score', 0))
            return row
        
        elif data_type == 'team':
            row = []
            if columns.get('id'): row.append(data['id'])
            if columns.get('teamname'): row.append(data['name'])
            if columns.get('email'): row.append(data['emails'])
            return row
        
        elif data_type == 'top_user':
            row = []
            if columns.get('rank'): row.append(data['rank'])
            if columns.get('id'): row.append(data['id'])
            if columns.get('username'): row.append(data['name'])
            if columns.get('email'): row.append(data['email'])
            if columns.get('score'): row.append(data['score'])
            return row
        
        elif data_type == 'top_team_member':
            row = []
            if columns.get('rank'): row.append(data['rank'])
            if columns.get('teamname'): row.append(data['team_name'])
            if columns.get('score'): row.append(data['team_score'])
            if columns.get('id'): row.append(data['user_id'])
            if columns.get('username'): row.append(data['username'])
            if columns.get('email'): row.append(data['email'])
            return row

        return []

    # ── Integration avec le plugin ctfd-score-override ───────────────────
    # Permet au TOP N de se baser sur le MEME classement que le scoreboard
    # public et les autres outils (rapports equipe, certificats) : score
    # natif + tiebreaker actif (temps/Fail%), ou les classements dedies
    # (Temps pur, Solo, Speed Runner, Presentiel) quand ils sont configures.
    # 'general' reste toujours disponible (fallback natif CTFd si le plugin
    # est absent ou qu'aucun tiebreaker n'y est actif).
    def get_ranking_bases():
        """{basis: {available, label}} — ce qui doit apparaitre dans le
        selecteur de base de classement du TOP N."""
        bases = {
            'general':    {'available': True,  'label': 'Général (score + tiebreaker actif)'},
            'native':     {'available': False, 'label': 'Classement CTFd natif (sans le plugin, score uniquement)'},
            'time':       {'available': False, 'label': 'Temps de résolution (A→B, score ignoré)'},
            'solo':       {'available': False, 'label': 'Solo (équipes à 1 joueur)'},
            'speedrun':   {'available': False, 'label': 'Speed Runner (fenêtre horaire, temps de solve pur)'},
            'presentiel': {'available': False, 'label': 'Présentiel (sélection admin)'},
        }
        mod = sys.modules.get('CTFd.plugins.ctfd-score-override')
        if mod is None:
            return bases
        try:
            settings = mod.ScoreOverrideSettings.query.first()
            if not settings:
                return bases
            tiebreaker_active = bool(
                (settings.time_enabled and settings.challenge_a_id and settings.challenge_b_id)
                or settings.enabled
            )
            # "Natif" n'a de sens comme option A PART que si un tiebreaker est
            # actif : sinon "Général" EST deja le classement natif (rien a
            # distinguer), pas la peine d'offrir un doublon.
            if tiebreaker_active:
                bases['native']['available'] = True
            if settings.time_enabled and settings.challenge_a_id and settings.challenge_b_id:
                bases['time']['available'] = True
            if get_mode_as_word() == 'teams' and mod._get_solo_map():
                bases['solo']['available'] = True
            if (
                settings.speedrun_enabled and settings.challenge_a_id and settings.challenge_b_id
                and settings.speedrun_window_start and settings.speedrun_window_end
            ):
                bases['speedrun']['available'] = True
            if mod._get_presentiel_ids():
                bases['presentiel']['available'] = True
        except Exception:
            pass
        return bases

    def get_ranked_account_ids(basis, exclude_hidden):
        """Liste ORDONNEE d'account_id pour la base de classement demandee.
        'general' reutilise le tri override-aware du plugin score-override
        quand il est present (coherent avec /scoreboard), sinon le tri natif
        CTFd. Les autres bases viennent integralement du plugin (vide si
        absent/non configure — le selecteur ne les propose alors pas, voir
        get_ranking_bases, mais on reste defensif ici aussi)."""
        from CTFd.utils.scores import get_standings

        mod = sys.modules.get('CTFd.plugins.ctfd-score-override')

        if basis == 'general':
            if mod is not None:
                try:
                    return list(mod._get_override_sorted_account_ids(admin=not exclude_hidden))
                except Exception:
                    pass
            return [int(s.account_id) for s in get_standings(admin=not exclude_hidden)]

        if basis == 'native':
            # Classement CTFd pur (score DESC, departage natif par date/id) —
            # ignore DELIBEREMENT le tiebreaker du plugin score-override meme
            # s'il est actif, contrairement a 'general' ci-dessus.
            return [int(s.account_id) for s in get_standings(admin=not exclude_hidden)]

        if mod is None:
            return []

        try:
            settings = mod.ScoreOverrideSettings.query.first()
            if not settings:
                return []

            visible_ids = None
            if exclude_hidden:
                visible_ids = {s.account_id for s in get_standings(admin=False)}

            if basis == 'time':
                if not (settings.time_enabled and settings.challenge_a_id and settings.challenge_b_id):
                    return []
                time_map = mod._get_time_map(settings.challenge_a_id, settings.challenge_b_id)
                ids = sorted(
                    (aid for aid, e in time_map.items() if e.get('b_solved')),
                    key=lambda aid: time_map[aid]['elapsed_seconds'],
                )
            elif basis == 'solo':
                solo_map = mod._get_solo_map()
                if not solo_map:
                    return []
                sorted_ids = mod._get_override_sorted_account_ids(admin=not exclude_hidden)
                ids = [aid for aid in sorted_ids if aid in solo_map]
            elif basis == 'speedrun':
                if not (
                    settings.speedrun_enabled and settings.challenge_a_id and settings.challenge_b_id
                    and settings.speedrun_window_start and settings.speedrun_window_end
                ):
                    return []
                # Volontairement PAS _get_speedrun_standings_data() ici : cette
                # fonction trie score DESC puis temps (coherent avec le vrai
                # classement du scoreboard), mais pour cette extraction le
                # "Speed Runner" doit se baser UNIQUEMENT sur le temps de
                # resolution A->B, score ignore — demande explicite, distinct
                # du Speed Runner du scoreboard public.
                speed_map = mod._get_speedrun_map(
                    settings.challenge_a_id, settings.challenge_b_id,
                    settings.speedrun_window_start, settings.speedrun_window_end,
                )
                ids = sorted(speed_map.keys(), key=lambda aid: speed_map[aid]['elapsed_seconds'])
            elif basis == 'presentiel':
                ids = [e['account_id'] for e in mod._get_presentiel_standings(admin=True)]
            else:
                return []

            if visible_ids is not None:
                ids = [aid for aid in ids if aid in visible_ids]
            return ids
        except Exception:
            return []

    # Création du Blueprint
    plugin_blueprint = Blueprint(
        'badges_data_extraction',
        __name__,
        template_folder='templates',
        static_folder='assets'
    )

    # Route principale pour afficher la page d'administration
    @plugin_blueprint.route('/admin/badges_extraction', methods=['GET'])
    @admins_only
    def admin_panel():
        """
        Menu du plugin : choix entre export manuel et export API Badgify
        """
        return render_template('menu.html', logo_url=_LOGO_URL)

    @plugin_blueprint.route('/admin/badges_extraction/manual', methods=['GET'])
    @admins_only
    def admin_panel_manual():
        """
        Page d'export manuel (export a la main)
        """
        return render_template('admin_panel.html', logo_url=_LOGO_URL)

    @plugin_blueprint.route('/admin/badges_extraction/award', methods=['GET'])
    @admins_only
    def admin_panel_award():
        """
        Page d'attribution via l'API Badgify : clone de l'export manuel en mode attribution
        """
        return render_template('admin_panel.html', award_mode=True, logo_url=_LOGO_URL)

    @plugin_blueprint.route('/admin/badges_extraction/badgify', methods=['GET'])
    @admins_only
    def admin_panel_api():
        """
        Page d'export via l'API Badgify (a venir)
        """
        return render_template('api_export.html', logo_url=_LOGO_URL)

    # Route API pour obtenir le mode CTFd
    @plugin_blueprint.route('/admin/badges_extraction/api/mode', methods=['GET'])
    @admins_only
    def get_ctfd_mode():
        """
        Retourne le mode CTFd (teams ou users)
        """
        try:
            mode = get_mode_as_word()
            return jsonify({
                'success': True,
                'mode': mode
            })
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour sauvegarder la configuration utilisateur
    @plugin_blueprint.route('/admin/badges_extraction/api/config', methods=['POST'])
    @admins_only
    def save_user_config():
        """
        Sauvegarde la configuration de l'utilisateur en base de données
        """
        try:
            from flask import session
            from CTFd.cache import clear_user_session
            import json
            
            data = request.get_json()
            user_id = session.get('id')
            
            if not user_id:
                return jsonify({
                    'success': False,
                    'error': 'User not authenticated'
                }), 401
            
            # Vérifier si une config existe déjà
            config = UserConfig.query.filter_by(user_id=user_id).first()
            
            if config:
                # Mettre à jour
                config.config_data = json.dumps(data)
                config.updated_at = db.func.current_timestamp()
            else:
                # Créer
                config = UserConfig(
                    user_id=user_id,
                    config_data=json.dumps(data)
                )
                db.session.add(config)
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Configuration sauvegardée'
            })
        
        except Exception as e:
            db.session.rollback()
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour charger la configuration utilisateur
    @plugin_blueprint.route('/admin/badges_extraction/api/config', methods=['GET'])
    @admins_only
    def load_user_config():
        """
        Charge la configuration sauvegardée de l'utilisateur
        """
        try:
            from flask import session
            import json
            
            user_id = session.get('id')
            
            if not user_id:
                return jsonify({
                    'success': False,
                    'error': 'User not authenticated'
                }), 401
            
            config = UserConfig.query.filter_by(user_id=user_id).first()
            
            if config:
                return jsonify({
                    'success': True,
                    'config': json.loads(config.config_data)
                })
            else:
                return jsonify({
                    'success': True,
                    'config': None
                })
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour supprimer la configuration utilisateur
    @plugin_blueprint.route('/admin/badges_extraction/api/config', methods=['DELETE'])
    @admins_only
    def delete_user_config():
        """
        Supprime la configuration sauvegardée de l'utilisateur
        """
        try:
            from flask import session
            
            user_id = session.get('id')
            
            if not user_id:
                return jsonify({
                    'success': False,
                    'error': 'User not authenticated'
                }), 401
            
            config = UserConfig.query.filter_by(user_id=user_id).first()
            
            if config:
                db.session.delete(config)
                db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Configuration réinitialisée'
            })
        
        except Exception as e:
            db.session.rollback()
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour récupérer les données des utilisateurs
    @plugin_blueprint.route('/admin/badges_extraction/api/users', methods=['GET'])
    @admins_only
    def get_users_data():
        """
        Récupère tous les utilisateurs avec leur username et email
        """
        try:
            exclude_admins = request.args.get('exclude_admins', 'false').lower() == 'true'
            exclude_hidden = request.args.get('exclude_hidden', 'false').lower() == 'true'
            challenge_id = request.args.get('challenge_id', None)
            
            users = Users.query.all()
            excluded_domains = ExcludedDomain.query.all()
            excluded_domains_list = [domain.domain.lower() for domain in excluded_domains]
            
            users_who_solved = set()
            if challenge_id:
                try:
                    challenge_id_int = int(challenge_id)
                    solves = Solves.query.filter_by(challenge_id=challenge_id_int).all()
                    users_who_solved = set(solve.user_id for solve in solves)
                except (ValueError, TypeError):
                    pass
            
            users_data = []
            excluded_count = 0
            
            for user in users:
                # Exclure les users hidden
                if exclude_hidden and user.hidden:
                    continue
                
                email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                
                if email_domain in excluded_domains_list:
                    excluded_count += 1
                    continue
                
                if exclude_admins and user.type == 'admin':
                    continue
                
                if challenge_id and user.id not in users_who_solved:
                    continue
                
                users_data.append({
                    'id': user.id,
                    'username': user.name,
                    'email': user.email,
                    'type': user.type,
                    'hidden': user.hidden
                })
            
            return jsonify({
                'success': True,
                'users': users_data,
                'count': len(users_data),
                'total_users': len(users),
                'excluded_count': excluded_count
            })
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour récupérer TOUS les utilisateurs sans exception
    @plugin_blueprint.route('/admin/badges_extraction/api/users/all', methods=['GET'])
    @admins_only
    def get_all_users_no_filter():
        """
        Récupère TOUS les utilisateurs sans aucun filtre (incluant admins et hidden)
        """
        try:
            users = Users.query.all()
            
            users_data = []
            for user in users:
                users_data.append({
                    'id': user.id,
                    'username': user.name,
                    'email': user.email,
                    'type': user.type,
                    'hidden': user.hidden
                })
            
            return jsonify({
                'success': True,
                'users': users_data,
                'count': len(users_data)
            })
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour récupérer les utilisateurs exclus
    @plugin_blueprint.route('/admin/badges_extraction/api/users/excluded', methods=['GET'])
    @admins_only
    def get_excluded_users():
        """
        Récupère les utilisateurs exclus
        """
        try:
            exclude_admins = request.args.get('exclude_admins', 'false').lower() == 'true'
            challenge_id = request.args.get('challenge_id', None)
            
            users = Users.query.all()
            excluded_domains = ExcludedDomain.query.all()
            excluded_domains_list = [domain.domain.lower() for domain in excluded_domains]
            
            users_who_solved = set()
            if challenge_id:
                try:
                    challenge_id_int = int(challenge_id)
                    solves = Solves.query.filter_by(challenge_id=challenge_id_int).all()
                    users_who_solved = set(solve.user_id for solve in solves)
                except (ValueError, TypeError):
                    pass
            
            excluded_users_data = []
            
            for user in users:
                email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                
                if email_domain not in excluded_domains_list:
                    continue
                
                if exclude_admins and user.type == 'admin':
                    continue
                
                if challenge_id and user.id not in users_who_solved:
                    continue
                
                excluded_users_data.append({
                    'id': user.id,
                    'username': user.name,
                    'email': user.email,
                    'excluded_domain': email_domain
                })
            
            return jsonify({
                'success': True,
                'users': excluded_users_data,
                'count': len(excluded_users_data)
            })
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour récupérer la liste des challenges
    @plugin_blueprint.route('/admin/badges_extraction/api/challenges', methods=['GET'])
    @admins_only
    def get_challenges():
        """
        Récupère la liste de tous les challenges de la plateforme
        """
        try:
            challenges = Challenges.query.filter_by(state='visible').all()
            challenges_list = [{'id': c.id, 'name': c.name, 'category': c.category} for c in challenges]
            
            return jsonify({
                'success': True,
                'challenges': challenges_list
            })
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour les bases de classement disponibles (integration
    # score-override) — alimente le selecteur du TOP N.
    @plugin_blueprint.route('/admin/badges_extraction/api/ranking_bases', methods=['GET'])
    @admins_only
    def ranking_bases():
        try:
            return jsonify({'success': True, 'bases': get_ranking_bases()})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # Route API pour l'aperçu 0% fail
    @plugin_blueprint.route('/admin/badges_extraction/api/preview/zero_fail', methods=['POST'])
    @admins_only
    def preview_zero_fail():
        """
        Aperçu des utilisateurs/teams avec 0% de fail
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id', None)
            
            results = []
            
            if mode == 'teams':
                teams = Teams.query.all()
                
                for team in teams:
                    if exclude_hidden and team.hidden:
                        continue
                    
                    team_members_ids = [member.id for member in team.members]
                    team_total_submissions = 0
                    team_successful_solves = 0
                    
                    for member_id in team_members_ids:
                        member_submissions = Submissions.query.filter_by(user_id=member_id).count()
                        team_total_submissions += member_submissions
                        member_solves = Submissions.query.filter_by(user_id=member_id, type='correct').count()
                        team_successful_solves += member_solves
                    
                    if team_successful_solves > 0 and team_total_submissions == team_successful_solves:
                        members_emails = []
                        members_pairs = []
                        for member in team.members:
                            email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                            if email_domain in excluded_domains_custom:
                                continue
                            if exclude_admins and member.type == 'admin':
                                continue
                            if challenge_id:
                                user_solved = Solves.query.filter_by(user_id=member.id, challenge_id=int(challenge_id)).first()
                                if not user_solved:
                                    continue
                            if member.email:
                                members_emails.append(member.email)
                                members_pairs.append({'username': member.name, 'email': member.email})

                        if members_emails:
                            results.append({
                                'id': team.id, 'name': team.name,
                                'emails': ', '.join(members_emails),
                                'members': members_pairs,
                            })
            else:
                users = Users.query.all()

                for user in users:
                    if exclude_hidden and user.hidden:
                        continue
                    
                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    if email_domain in excluded_domains_custom:
                        continue
                    if exclude_admins and user.type == 'admin':
                        continue
                    if challenge_id:
                        user_solved = Solves.query.filter_by(user_id=user.id, challenge_id=int(challenge_id)).first()
                        if not user_solved:
                            continue
                    
                    user_total_submissions = Submissions.query.filter_by(user_id=user.id).count()
                    user_successful_solves = Submissions.query.filter_by(user_id=user.id, type='correct').count()
                    
                    if user_successful_solves > 0 and user_total_submissions == user_successful_solves:
                        results.append({'id': user.id, 'name': user.name, 'email': user.email})
            
            return jsonify({'success': True, 'mode': mode, 'results': results})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    # Route API pour l'aperçu TOP N
    @plugin_blueprint.route('/admin/badges_extraction/api/preview/top_n', methods=['POST'])
    @admins_only
    def preview_top_n():
        """
        Aperçu des TOP N teams/users
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            top_limit = int(data.get('top_limit', 3))
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id', None)
            basis = data.get('ranking_basis', 'general')

            results = []
            from CTFd.utils.scores import get_standings

            ranked_ids = get_ranked_account_ids(basis, exclude_hidden)
            score_lookup = {int(s.account_id): int(s.score) for s in get_standings(admin=True)}

            if mode == 'teams':
                rank = 0
                for account_id in ranked_ids:
                    if rank >= top_limit:
                        break

                    team = Teams.query.get(account_id)
                    if not team:
                        continue

                    if exclude_hidden and team.hidden:
                        continue

                    # Incrémenter le rank AVANT de filtrer les membres
                    rank += 1

                    members_emails = []
                    members_pairs = []
                    for member in team.members:
                        email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                        if email_domain in excluded_domains_custom:
                            continue
                        if exclude_admins and member.type == 'admin':
                            continue
                        if challenge_id:
                            user_solved = Solves.query.filter_by(user_id=member.id, challenge_id=int(challenge_id)).first()
                            if not user_solved:
                                continue
                        if member.email:
                            members_emails.append(member.email)
                            members_pairs.append({'username': member.name, 'email': member.email})

                    # Ajouter la team même si aucun membre valide (afficher le rang)
                    results.append({
                        'rank': rank,
                        'id': team.id,
                        'name': team.name,
                        'score': score_lookup.get(account_id, 0),
                        'emails': ', '.join(members_emails) if members_emails else 'Aucun membre après filtrage',
                        'members': members_pairs,
                    })
            else:
                rank = 0
                for account_id in ranked_ids:
                    if rank >= top_limit:
                        break

                    user = Users.query.get(account_id)
                    if not user:
                        continue

                    if exclude_hidden and user.hidden:
                        continue

                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    if email_domain in excluded_domains_custom:
                        continue
                    if exclude_admins and user.type == 'admin':
                        continue
                    if challenge_id:
                        user_solved = Solves.query.filter_by(user_id=user.id, challenge_id=int(challenge_id)).first()
                        if not user_solved:
                            continue

                    rank += 1
                    results.append({
                        'rank': rank,
                        'id': user.id,
                        'name': user.name,
                        'score': score_lookup.get(account_id, 0),
                        'email': user.email
                    })

            return jsonify({'success': True, 'mode': mode, 'results': results})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    # Route API pour l'aperçu No Hint
    @plugin_blueprint.route('/admin/badges_extraction/api/preview/no_hint', methods=['POST'])
    @admins_only
    def preview_no_hint():
        """
        Aperçu des utilisateurs/teams sans hints
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id', None)
            
            results = []
            
            if mode == 'teams':
                teams = Teams.query.all()
                for team in teams:
                    if exclude_hidden and team.hidden:
                        continue
                    
                    has_hint = False
                    team_awards = Awards.query.filter_by(team_id=team.id).all()
                    for award in team_awards:
                        if award.value < 0:
                            has_hint = True
                            break
                    
                    if not has_hint:
                        for member in team.members:
                            member_awards = Awards.query.filter_by(user_id=member.id).all()
                            for award in member_awards:
                                if award.value < 0:
                                    has_hint = True
                                    break
                            if has_hint:
                                break
                    
                    if not has_hint and len(team.members) > 0:
                        members_emails = []
                        members_pairs = []
                        for member in team.members:
                            email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                            if email_domain in excluded_domains_custom:
                                continue
                            if exclude_admins and member.type == 'admin':
                                continue
                            if challenge_id:
                                user_solved = Solves.query.filter_by(user_id=member.id, challenge_id=int(challenge_id)).first()
                                if not user_solved:
                                    continue
                            if member.email:
                                members_emails.append(member.email)
                                members_pairs.append({'username': member.name, 'email': member.email})

                        if members_emails:
                            results.append({
                                'id': team.id, 'name': team.name,
                                'emails': ', '.join(members_emails),
                                'members': members_pairs,
                            })
            else:
                users = Users.query.all()
                for user in users:
                    if exclude_hidden and user.hidden:
                        continue
                    
                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    if email_domain in excluded_domains_custom:
                        continue
                    if exclude_admins and user.type == 'admin':
                        continue
                    if challenge_id:
                        user_solved = Solves.query.filter_by(user_id=user.id, challenge_id=int(challenge_id)).first()
                        if not user_solved:
                            continue
                    
                    user_awards = Awards.query.filter_by(user_id=user.id).all()
                    has_hint = any(award.value < 0 for award in user_awards)
                    if not has_hint:
                        results.append({'id': user.id, 'name': user.name, 'email': user.email})
            
            return jsonify({'success': True, 'mode': mode, 'results': results})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    # Route API pour récupérer les domaines exclus
    @plugin_blueprint.route('/admin/badges_extraction/api/excluded_domains', methods=['GET'])
    @admins_only
    def get_excluded_domains():
        """
        Récupère la liste de tous les domaines exclus
        """
        try:
            excluded_domains = ExcludedDomain.query.all()
            domains_list = [{'id': domain.id, 'domain': domain.domain} for domain in excluded_domains]
            
            return jsonify({
                'success': True,
                'domains': domains_list
            })
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour ajouter un domaine exclu
    @plugin_blueprint.route('/admin/badges_extraction/api/excluded_domains', methods=['POST'])
    @admins_only
    def add_excluded_domain():
        """
        Ajoute un nouveau domaine à la liste d'exclusion
        """
        try:
            data = request.get_json()
            domain = data.get('domain', '').strip().lower()
            
            if not domain:
                return jsonify({
                    'success': False,
                    'error': 'Le domaine ne peut pas être vide'
                }), 400
            
            existing_domain = ExcludedDomain.query.filter_by(domain=domain).first()
            if existing_domain:
                return jsonify({
                    'success': False,
                    'error': 'Ce domaine existe déjà dans la liste'
                }), 400
            
            new_domain = ExcludedDomain(domain=domain)
            db.session.add(new_domain)
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': f'Domaine {domain} ajouté avec succès',
                'domain': {'id': new_domain.id, 'domain': new_domain.domain}
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route API pour supprimer un domaine exclu
    @plugin_blueprint.route('/admin/badges_extraction/api/excluded_domains/<int:domain_id>', methods=['DELETE'])
    @admins_only
    def delete_excluded_domain(domain_id):
        """
        Supprime un domaine de la liste d'exclusion
        """
        try:
            domain = ExcludedDomain.query.get(domain_id)
            
            if not domain:
                return jsonify({
                    'success': False,
                    'error': 'Domaine non trouvé'
                }), 404
            
            db.session.delete(domain)
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': f'Domaine {domain.domain} supprimé avec succès'
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route pour l'extraction globale (CSV ou ZIP selon le nombre)
    @plugin_blueprint.route('/admin/badges_extraction/api/export/global', methods=['POST'])
    @admins_only
    def export_global():
        """
        Exporte tous les utilisateurs filtrés en CSV (si <50) ou ZIP (si >=50)
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            challenge_id = data.get('challenge_id', None)
            excluded_domains_custom = data.get('excluded_domains', [])
            columns = data.get('columns', {
                'id': True, 'rank': True, 'name': True, 'email': True, 'score': True
            })
            
            users = Users.query.all()
            
            users_who_solved = set()
            if challenge_id:
                try:
                    challenge_id_int = int(challenge_id)
                    solves = Solves.query.filter_by(challenge_id=challenge_id_int).all()
                    users_who_solved = set(solve.user_id for solve in solves)
                except (ValueError, TypeError):
                    pass
            
            filtered_users = []
            for user in users:
                # Appliquer les filtres automatiques
                if exclude_hidden and user.hidden:
                    continue
                
                email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                
                if email_domain in excluded_domains_custom:
                    continue
                
                if exclude_admins and user.type == 'admin':
                    continue
                
                if challenge_id and user.id not in users_who_solved:
                    continue
                
                filtered_users.append(user)
            
            total_users = len(filtered_users)
            
            # Si moins de 50 utilisateurs : CSV simple
            if total_users <= 50:
                output = io.StringIO()
                writer = csv.writer(output)
                
                # Header dynamique
                writer.writerow(build_csv_row(columns, 'header_user', None))
                
                # Données
                for user in filtered_users:
                    writer.writerow(build_csv_row(columns, 'user', {
                        'id': user.id,
                        'name': user.name,
                        'email': user.email,
                        'score': user.score
                    }))
                
                output.seek(0)
                
                filename = f"extraction_global_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                
                return send_file(
                    io.BytesIO(output.getvalue().encode('utf-8')),
                    mimetype='text/csv',
                    as_attachment=True,
                    download_name=filename
                )
            
            # Si 50 utilisateurs ou plus : ZIP avec plusieurs CSV
            else:
                zip_buffer = io.BytesIO()
                batch_size = 50
                
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    # Découper en lots de 50
                    for batch_index in range(0, total_users, batch_size):
                        batch_users = filtered_users[batch_index:batch_index + batch_size]
                        
                        csv_output = io.StringIO()
                        writer = csv.writer(csv_output)
                        
                        # Header
                        writer.writerow(build_csv_row(columns, 'header_user', None))
                        
                        # Données du lot
                        for user in batch_users:
                            writer.writerow(build_csv_row(columns, 'user', {
                                'id': user.id,
                                'name': user.name,
                                'email': user.email,
                                'score': user.score
                            }))
                        
                        # Nom du fichier dans le ZIP
                        start_num = batch_index + 1
                        end_num = min(batch_index + batch_size, total_users)
                        csv_filename = f"joueurs_{start_num}_a_{end_num}.csv"
                        
                        zip_file.writestr(csv_filename, csv_output.getvalue())
                
                zip_buffer.seek(0)
                
                filename = f"extraction_global_{total_users}_joueurs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
                
                return send_file(
                    zip_buffer,
                    mimetype='application/zip',
                    as_attachment=True,
                    download_name=filename
                )
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route pour l'extraction 0% fail
    @plugin_blueprint.route('/admin/badges_extraction/api/export/zero_fail', methods=['POST'])
    @admins_only
    def export_zero_fail():
        """
        Exporte les utilisateurs/teams avec 0% de fail
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id', None)
            columns = data.get('columns', {
                'id': True, 'rank': True, 'username': True, 'teamname': True, 'email': True, 'score': True
            })
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            if mode == 'teams':
                writer.writerow(build_csv_row(columns, 'header_team', None))
                
                teams = Teams.query.all()
                
                for team in teams:
                    if exclude_hidden and team.hidden:
                        continue
                    
                    team_members_ids = [member.id for member in team.members]
                    team_total_submissions = 0
                    team_successful_solves = 0
                    
                    for member_id in team_members_ids:
                        member_submissions = Submissions.query.filter_by(user_id=member_id).count()
                        team_total_submissions += member_submissions
                        member_solves = Submissions.query.filter_by(user_id=member_id, type='correct').count()
                        team_successful_solves += member_solves
                    
                    if team_successful_solves > 0 and team_total_submissions == team_successful_solves:
                        # Une ligne par membre
                        for member in team.members:
                            email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                            
                            if email_domain in excluded_domains_custom:
                                continue
                            
                            if exclude_admins and member.type == 'admin':
                                continue
                            
                            if challenge_id:
                                user_solved = Solves.query.filter_by(user_id=member.id, challenge_id=int(challenge_id)).first()
                                if not user_solved:
                                    continue
                            
                            # Écrire une ligne par membre avec team_id, team_name, username, email
                            row = []
                            if columns.get('id'): row.append(team.id)
                            if columns.get('teamname'): row.append(team.name)
                            if columns.get('username'): row.append(member.name)
                            if columns.get('email'): row.append(member.email)
                            writer.writerow(row)
            
            else:
                writer.writerow(build_csv_row(columns, 'header_user', None))
                
                users = Users.query.all()
                
                users_who_solved = set()
                if challenge_id:
                    solves = Solves.query.filter_by(challenge_id=int(challenge_id)).all()
                    users_who_solved = set(solve.user_id for solve in solves)
                
                zero_fail_users = []
                
                for user in users:
                    # Appliquer les filtres automatiques
                    if exclude_hidden and user.hidden:
                        continue
                    
                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    
                    if email_domain in excluded_domains_custom:
                        continue
                    
                    if exclude_admins and user.type == 'admin':
                        continue
                    
                    if challenge_id and user.id not in users_who_solved:
                        continue
                    
                    user_total_submissions = Submissions.query.filter_by(user_id=user.id).count()
                    user_successful_solves = Submissions.query.filter_by(user_id=user.id, type='correct').count()
                    
                    if user_successful_solves > 0 and user_total_submissions == user_successful_solves:
                        zero_fail_users.append(user)
                
                for user in zero_fail_users:
                    writer.writerow(build_csv_row(columns, 'user', {
                        'id': user.id,
                        'name': user.name,
                        'email': user.email,
                        'score': user.score
                    }))
            
            output.seek(0)
            
            filename = f"extraction_zero_fail_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            
            return send_file(
                io.BytesIO(output.getvalue().encode('utf-8')),
                mimetype='text/csv',
                as_attachment=True,
                download_name=filename
            )
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route pour l'extraction TOP N (ZIP avec plusieurs CSV)
    @plugin_blueprint.route('/admin/badges_extraction/api/export/top_n', methods=['POST'])
    @admins_only
    def export_top_n():
        """
        Exporte les TOP N teams/users dans un ZIP contenant un CSV par position
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            top_limit = int(data.get('top_limit', 3))
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id', None)
            basis = data.get('ranking_basis', 'general')
            columns = data.get('columns', {
                'id': True, 'rank': True, 'username': True, 'teamname': True, 'email': True, 'score': True
            })

            from CTFd.utils.scores import get_standings

            ranked_ids = get_ranked_account_ids(basis, exclude_hidden)
            score_lookup = {int(s.account_id): int(s.score) for s in get_standings(admin=True)}

            zip_buffer = io.BytesIO()

            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:

                if mode == 'teams':
                    added_teams = 0
                    for account_id in ranked_ids:
                        if added_teams >= top_limit:
                            break

                        team = Teams.query.get(account_id)
                        if not team:
                            continue

                        if exclude_hidden and team.hidden:
                            continue

                        # Incrémenter AVANT de filtrer les membres
                        added_teams += 1

                        csv_output = io.StringIO()
                        writer = csv.writer(csv_output)
                        writer.writerow(build_csv_row(columns, 'header_top_team', None))

                        # Ajouter tous les membres valides
                        for member in team.members:
                            email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''

                            if email_domain in excluded_domains_custom:
                                continue

                            if exclude_admins and member.type == 'admin':
                                continue

                            if challenge_id:
                                user_solved = Solves.query.filter_by(user_id=member.id, challenge_id=int(challenge_id)).first()
                                if not user_solved:
                                    continue

                            writer.writerow(build_csv_row(columns, 'top_team_member', {
                                'team_name': team.name,
                                'team_score': score_lookup.get(account_id, 0),
                                'rank': added_teams,
                                'user_id': member.id,
                                'username': member.name,
                                'email': member.email
                            }))

                        # Toujours créer le CSV même sans membres (pour montrer le rang)
                        csv_filename = f"top_{added_teams}_{team.name.replace(' ', '_')}.csv"
                        zip_file.writestr(csv_filename, csv_output.getvalue())

                else:
                    added_users = 0
                    for account_id in ranked_ids:
                        if added_users >= top_limit:
                            break

                        user = Users.query.get(account_id)
                        if not user:
                            continue

                        # Appliquer les filtres automatiques normalement
                        if exclude_hidden and user.hidden:
                            continue

                        email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''

                        if email_domain in excluded_domains_custom:
                            continue

                        if exclude_admins and user.type == 'admin':
                            continue

                        if challenge_id:
                            user_solved = Solves.query.filter_by(user_id=user.id, challenge_id=int(challenge_id)).first()
                            if not user_solved:
                                continue

                        added_users += 1

                        csv_output = io.StringIO()
                        writer = csv.writer(csv_output)
                        writer.writerow(build_csv_row(columns, 'header_top_user', None))
                        writer.writerow(build_csv_row(columns, 'top_user', {
                            'rank': added_users,
                            'id': user.id,
                            'name': user.name,
                            'email': user.email,
                            'score': score_lookup.get(account_id, 0)
                        }))

                        csv_filename = f"top_{added_users}_{user.name.replace(' ', '_')}.csv"
                        zip_file.writestr(csv_filename, csv_output.getvalue())
            
            zip_buffer.seek(0)
            
            filename = f"extraction_top_{top_limit}_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            
            return send_file(
                zip_buffer,
                mimetype='application/zip',
                as_attachment=True,
                download_name=filename
            )
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # ── Onglet "Résolu le challenge X" ─────────────────────────────────────
    # Liste brute des equipes/joueurs ayant resolu UN challenge donne — utile
    # par exemple pour exporter qui a termine tout le CTF en choisissant le
    # dernier challenge d'un parcours comme filtre.
    @plugin_blueprint.route('/admin/badges_extraction/api/preview/challenge_solved', methods=['POST'])
    @admins_only
    def preview_challenge_solved():
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id')

            if not challenge_id:
                return jsonify({'success': False, 'error': 'challenge_id requis'}), 400
            challenge_id = int(challenge_id)

            results = []

            if mode == 'teams':
                solved_team_ids = {
                    s.team_id for s in Solves.query.filter_by(challenge_id=challenge_id)
                    .filter(Solves.team_id.isnot(None)).all()
                }
                teams = Teams.query.filter(Teams.id.in_(solved_team_ids)).all() if solved_team_ids else []
                for team in teams:
                    if exclude_hidden and team.hidden:
                        continue
                    members_emails = []
                    members_pairs = []
                    for member in team.members:
                        email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                        if email_domain in excluded_domains_custom:
                            continue
                        if exclude_admins and member.type == 'admin':
                            continue
                        if member.email:
                            members_emails.append(member.email)
                            members_pairs.append({'username': member.name, 'email': member.email})
                    results.append({
                        'id': team.id,
                        'name': team.name,
                        'emails': ', '.join(members_emails) if members_emails else 'Aucun membre après filtrage',
                        'members': members_pairs,
                    })
            else:
                solved_user_ids = {
                    s.user_id for s in Solves.query.filter_by(challenge_id=challenge_id)
                    .filter(Solves.user_id.isnot(None)).all()
                }
                users = Users.query.filter(Users.id.in_(solved_user_ids)).all() if solved_user_ids else []
                for user in users:
                    if exclude_hidden and user.hidden:
                        continue
                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    if email_domain in excluded_domains_custom:
                        continue
                    if exclude_admins and user.type == 'admin':
                        continue
                    results.append({'id': user.id, 'name': user.name, 'email': user.email})

            return jsonify({'success': True, 'mode': mode, 'results': results})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    @plugin_blueprint.route('/admin/badges_extraction/api/export/challenge_solved', methods=['POST'])
    @admins_only
    def export_challenge_solved():
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id')
            columns = data.get('columns', {
                'id': True, 'username': True, 'teamname': True, 'email': True
            })

            if not challenge_id:
                return jsonify({'success': False, 'error': 'challenge_id requis'}), 400
            challenge_id = int(challenge_id)

            challenge = Challenges.query.get(challenge_id)
            challenge_name = challenge.name if challenge else str(challenge_id)

            output = io.StringIO()
            writer = csv.writer(output)

            if mode == 'teams':
                writer.writerow(build_csv_row(columns, 'header_team', None))
                solved_team_ids = {
                    s.team_id for s in Solves.query.filter_by(challenge_id=challenge_id)
                    .filter(Solves.team_id.isnot(None)).all()
                }
                teams = Teams.query.filter(Teams.id.in_(solved_team_ids)).all() if solved_team_ids else []
                for team in teams:
                    if exclude_hidden and team.hidden:
                        continue
                    for member in team.members:
                        email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                        if email_domain in excluded_domains_custom:
                            continue
                        if exclude_admins and member.type == 'admin':
                            continue
                        row = []
                        if columns.get('id'): row.append(team.id)
                        if columns.get('teamname'): row.append(team.name)
                        if columns.get('username'): row.append(member.name)
                        if columns.get('email'): row.append(member.email)
                        writer.writerow(row)
            else:
                writer.writerow(build_csv_row(columns, 'header_user', None))
                solved_user_ids = {
                    s.user_id for s in Solves.query.filter_by(challenge_id=challenge_id)
                    .filter(Solves.user_id.isnot(None)).all()
                }
                users = Users.query.filter(Users.id.in_(solved_user_ids)).all() if solved_user_ids else []
                for user in users:
                    if exclude_hidden and user.hidden:
                        continue
                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    if email_domain in excluded_domains_custom:
                        continue
                    if exclude_admins and user.type == 'admin':
                        continue
                    writer.writerow(build_csv_row(columns, 'user', {
                        'id': user.id, 'name': user.name, 'email': user.email
                    }))

            output.seek(0)

            import re as _re
            safe_name = _re.sub(r'[^a-zA-Z0-9_-]+', '_', challenge_name)[:40]
            filename = f"solved_{safe_name}_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

            return send_file(
                io.BytesIO(output.getvalue().encode('utf-8')),
                mimetype='text/csv',
                as_attachment=True,
                download_name=filename
            )
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # Route pour l'extraction No Hint
    @plugin_blueprint.route('/admin/badges_extraction/api/export/no_hint', methods=['POST'])
    @admins_only
    def export_no_hint():
        """
        Exporte les utilisateurs/teams n'ayant utilisé aucun hint
        """
        try:
            data = request.get_json()
            mode = data.get('mode', get_mode_as_word())
            exclude_admins = data.get('exclude_admins', False)
            exclude_hidden = data.get('exclude_hidden', False)
            excluded_domains_custom = data.get('excluded_domains', [])
            challenge_id = data.get('challenge_id', None)
            columns = data.get('columns', {
                'id': True, 'rank': True, 'username': True, 'teamname': True, 'email': True, 'score': True
            })
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            if mode == 'teams':
                writer.writerow(build_csv_row(columns, 'header_team', None))
                
                teams = Teams.query.all()
                
                for team in teams:
                    if exclude_hidden and team.hidden:
                        continue
                    
                    has_hint = False
                    team_awards = Awards.query.filter_by(team_id=team.id).all()
                    for award in team_awards:
                        if award.value < 0:
                            has_hint = True
                            break
                    
                    if not has_hint:
                        for member in team.members:
                            member_awards = Awards.query.filter_by(user_id=member.id).all()
                            for award in member_awards:
                                if award.value < 0:
                                    has_hint = True
                                    break
                            if has_hint:
                                break
                    
                    if not has_hint and len(team.members) > 0:
                        # Une ligne par membre
                        for member in team.members:
                            email_domain = member.email.split('@')[-1].lower() if member.email and '@' in member.email else ''
                            if email_domain in excluded_domains_custom:
                                continue
                            if exclude_admins and member.type == 'admin':
                                continue
                            if challenge_id:
                                user_solved = Solves.query.filter_by(user_id=member.id, challenge_id=int(challenge_id)).first()
                                if not user_solved:
                                    continue
                            
                            # Écrire une ligne par membre
                            row = []
                            if columns.get('id'): row.append(team.id)
                            if columns.get('teamname'): row.append(team.name)
                            if columns.get('username'): row.append(member.name)
                            if columns.get('email'): row.append(member.email)
                            writer.writerow(row)
            
            else:
                writer.writerow(build_csv_row(columns, 'header_user', None))
                
                users = Users.query.all()
                
                users_who_solved = set()
                if challenge_id:
                    solves = Solves.query.filter_by(challenge_id=int(challenge_id)).all()
                    users_who_solved = set(solve.user_id for solve in solves)
                
                no_hint_users = []
                
                for user in users:
                    # Appliquer les filtres automatiques
                    if exclude_hidden and user.hidden:
                        continue
                    
                    email_domain = user.email.split('@')[-1].lower() if user.email and '@' in user.email else ''
                    if email_domain in excluded_domains_custom:
                        continue
                    if exclude_admins and user.type == 'admin':
                        continue
                    if challenge_id and user.id not in users_who_solved:
                        continue
                    
                    user_awards = Awards.query.filter_by(user_id=user.id).all()
                    has_hint = any(award.value < 0 for award in user_awards)
                    if not has_hint:
                        no_hint_users.append(user)
                
                for user in no_hint_users:
                    writer.writerow(build_csv_row(columns, 'user', {
                        'id': user.id,
                        'name': user.name,
                        'email': user.email,
                        'score': user.score
                    }))
            
            output.seek(0)
            
            filename = f"extraction_no_hint_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            
            return send_file(
                io.BytesIO(output.getvalue().encode('utf-8')),
                mimetype='text/csv',
                as_attachment=True,
                download_name=filename
            )
        
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    # ============ Export API Badgify ============
    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/config', methods=['GET'])
    @admins_only
    def badgify_get_config():
        cfg = BadgifyConfig.query.first()
        if not cfg:
            return jsonify({'success': True, 'configured': False,
                            'api_url': 'https://badgify.eu/api/v1/public'})
        return jsonify({'success': True, 'configured': True,
                        'api_url': cfg.api_url})

    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/config', methods=['POST'])
    @admins_only
    def badgify_save_config():
        data = request.get_json(silent=True) or {}
        token = (data.get('token') or '').strip()
        api_url = (data.get('api_url') or 'https://badgify.eu/api/v1/public').strip()
        if not token:
            return jsonify({'success': False, 'error': 'Token requis'}), 422
        if not api_url.startswith('https://'):
            return jsonify({'success': False, 'error': 'URL API invalide (https requis)'}), 422
        cfg = BadgifyConfig.query.first()
        if not cfg:
            cfg = BadgifyConfig(api_url=api_url, token=token)
            db.session.add(cfg)
        else:
            cfg.api_url = api_url
            cfg.token = token
        db.session.commit()
        return jsonify({'success': True, 'configured': True,
                        'api_url': cfg.api_url})

    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/config', methods=['DELETE'])
    @admins_only
    def badgify_delete_config():
        cfg = BadgifyConfig.query.first()
        if cfg:
            db.session.delete(cfg)
            db.session.commit()
        return jsonify({'success': True, 'configured': False})

    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/test', methods=['POST'])
    @admins_only
    def badgify_test():
        cfg = BadgifyConfig.query.first()
        if not cfg:
            return jsonify({'success': False, 'error': 'Non configuré'}), 400
        code, body = _badgify_call(cfg, '/ping')
        ok = (code == 200)
        msg = "Connexion réussie à l'API Badgify." if ok else (_badgify_friendly(code) or ("Erreur HTTP %s" % code))
        info = body if (ok and isinstance(body, dict)) else None
        if isinstance(info, dict) and isinstance(info.get('token'), dict):
            info['token'] = {k: v for k, v in info['token'].items() if k != 'last4'}
        return jsonify({'success': ok, 'status_code': code, 'message': msg, 'info': info})

    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/badges', methods=['POST'])
    @admins_only
    def badgify_badges():
        cfg = BadgifyConfig.query.first()
        if not cfg:
            return jsonify({'success': False, 'error': 'Non configuré'}), 400
        code, body = _badgify_call(cfg, '/badges')
        if code != 200:
            return jsonify({'success': False, 'status_code': code,
                            'error': _badgify_friendly(code) or ("Erreur HTTP %s" % code)})
        badges = body.get('badges') if isinstance(body, dict) else None
        return jsonify({'success': True, 'badges': badges if badges is not None else body})

    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/stats', methods=['POST'])
    @admins_only
    def badgify_stats():
        cfg = BadgifyConfig.query.first()
        if not cfg:
            return jsonify({'success': False, 'error': 'Non configuré'}), 400
        code, body = _badgify_call(cfg, '/stats')
        if code != 200:
            return jsonify({'success': False, 'status_code': code,
                            'error': _badgify_friendly(code) or ("Erreur HTTP %s" % code)})
        badges = body.get('badges') if isinstance(body, dict) else body
        return jsonify({'success': True, 'badges': badges})

    @plugin_blueprint.route('/admin/badges_extraction/api/badgify/award', methods=['POST'])
    @admins_only
    def badgify_award():
        cfg = BadgifyConfig.query.first()
        if not cfg:
            return jsonify({'success': False, 'error': 'Non configuré'}), 400
        data = request.get_json(silent=True) or {}
        badge_id = (data.get('badge_id') or '').strip()
        recipients = data.get('recipients') or []
        if not badge_id:
            return jsonify({'success': False, 'error': 'badge_id requis'}), 422
        if not recipients:
            return jsonify({'success': False, 'error': 'Aucun destinataire'}), 422
        if len(recipients) > 50:
            return jsonify({'success': False, 'error': 'Maximum 50 destinataires par envoi'}), 422
        code, body = _badgify_call(cfg, '/badges/%s/award' % badge_id, {'recipients': recipients})
        if code == 200:
            return jsonify({'success': True, 'result': body})
        return jsonify({'success': False, 'status_code': code,
                        'error': _badgify_friendly(code) or ("Erreur HTTP %s" % code), 'result': body})

    # Enregistrement du Blueprint dans l'application Flask
    app.register_blueprint(plugin_blueprint)

    # Enregistrement du dossier assets si vous avez des fichiers statiques (CSS, JS)
    register_plugin_assets_directory(
        app, 
        base_path='/plugins/%s/images/' % _PLUGIN_DIR
    )
