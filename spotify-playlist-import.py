#!/usr/bin/env python
# -*- coding: utf-8 -*-

import eyed3
import argparse
import os
import sys
import logging
import logging.handlers
import plistlib
import spotipy
import spotipy.util as util
from difflib import SequenceMatcher
from termcolor import colored

logger = logging.getLogger(__name__)


def parse_arguments():
    p = argparse.ArgumentParser(description='A script for migrate your library from Apple Music or import a m3u playlist into Spotify')
    p.add_argument('-f', '--file', help='Path to M3U playlist file or iTunes XML file', type=argparse.FileType('r'),
                   required=True)
    p.add_argument('-u', '--username', help='Spotify username', required=True)
    p.add_argument('-d', '--debug', help='Debug mode', action='store_true', default=False)
    return p.parse_args()


def load_playlist_file(playlist_file):
    tracks = []

    def _parse_m3u():
        content = [line.strip() for line in playlist_file if line.strip() and not line.startswith("#")]

        for track in content:
            tracks.append({'path': track})

    def _parse_xml():
        # 'readPlist' is deprecated but new method available only in Python 3
        playlist = plistlib.readPlist(playlist_file)
        for track_meta in playlist['Tracks'].values():
            artist = track_meta['Artist'] if 'Artist' in track_meta else ''
            name = track_meta['Name'] if 'Name' in track_meta else ''
            tracks.append({'artist': artist, 'name': name, 'source': 'itunes'})

    path_with_name, file_ext = os.path.splitext(playlist_file.name)
    read_id3_tags = False
    try:
        if file_ext == '.m3u':
            _parse_m3u()
            read_id3_tags = True
        elif file_ext == '.xml':
            _parse_xml()
            # If we have iTunes XML we don't need read ID3 tags because we have all information
        else:
            print colored('Playlist file failed load: Unsupported file extension "%s"', 'red')
            sys.exit(1)
    except Exception as e:
        logger.critical('Playlist file "%s" failed load: %s' % (playlist_file, str(e)))
        logger.critical('Playlist file "%s" failed load: %s' % (playlist_file.name, str(e)))
        sys.exit(1)
    else:
        return {'tracks': tracks, 'read_id3': read_id3_tags}


def read_id3_tags(file_name):
    tag_data = None
    try:
        track_id3 = eyed3.load(file_name)
    except Exception as e:
        logger.debug('Track "%s" failed ID3 tag load: %s' % (file_name, str(e)))
    else:
        logger.debug('Reading tags from "%s"' % file_name)
        if track_id3.tag is not None:
            if track_id3.tag.artist is not None and track_id3.tag.title is not None:
                tag_data = {'artist': track_id3.tag.artist, 'name': track_id3.tag.title}
    return tag_data


def guess_missing_track_info(file_name):
    guess = None
    filename = os.path.basename(file_name)
    filename_no_ext = os.path.splitext(filename)[0]
    track_uri_parts = filename_no_ext.split('-')
    if len(track_uri_parts) > 1:
        guess = {'artist': track_uri_parts[0].strip(),
                 'name': track_uri_parts[1].strip()}
    return guess


def find_spotify_track(sp, track):
    def _select_result_from_spotify_search(search_string, track_name, spotify_match_threshold):
        logger.debug('Searching Spotify for "%s" trying to find track called "%s"' % (search_string, track_name))

        def _how_similar(a, b):
            return SequenceMatcher(None, a, b).ratio()

        results_raw = sp.search(q=search_string, limit=30)
        if len(results_raw['tracks']['items']) > 0:
            spotify_results = results_raw['tracks']['items']
            logger.debug('Spotify results:%s' % len(spotify_results))
            for spotify_result in spotify_results:
                spotify_result['rank'] = _how_similar(track_name, spotify_result['name'])
                if spotify_result['rank'] == 1.0:
                    return {'id': spotify_result['id'], 'name': spotify_result['name'],
                            'artist': spotify_result['artists'][0]['name']}
            spotify_results_sorted = sorted(spotify_results, key=lambda k: k['rank'], reverse=True)
            if len(spotify_results_sorted) > 0 and spotify_results_sorted[0]['rank'] > spotify_match_threshold:
                return {'id': spotify_results_sorted[0]['id'], 'name': spotify_results_sorted[0]['name'],
                        'artist': spotify_results_sorted[0]['artists'][0]['name']}
        logger.debug('No good Spotify result found')
        return False

    spotify_match_threshold = 0.5
 
    if 'artist' in track and 'name' in track:
        spotify_search_string = '%s %s' % (track['artist'], track['name'])
        search_result = _select_result_from_spotify_search(
            spotify_search_string,
            track['name'],
            spotify_match_threshold
        )

        if search_result:
            return search_result
    
    return False


def format_track_info(track):
    if track['source'] == 'id3':
        formatted_id3_data = '%s - %s' % (repr(track['artist']), repr(track['name']))
        formatted_guess = 'Not required'
    else:
        formatted_id3_data = colored('None', 'red')
        if track['source'] == 'guess':
            formatted_guess = '%s - %s' % (repr(track['artist']), repr(track['name']))
        else:
            formatted_guess = colored('None', 'red')
    if track['spotify_data']:
        formatted_spotify = colored('%s - %s, %s' % (
                                    repr(track['spotify_data']['artist']),
                                    repr(track['spotify_data']['name']), repr(track['spotify_data']['id'])),
                                    'green')
    else:
        formatted_spotify = colored('None', 'red')
    return '\n%s\nIDv3 tag data: %s\nGuess from filename: %s\nSpotify: %s' % (
        colored(repr(track['path']), 'blue') if track['source'] != 'itunes' else colored(repr(track['artist'] + ' - ' + track['name']), 'blue'),
        formatted_id3_data,
        formatted_guess,
        formatted_spotify
    )


def init_credentials_manager():
    client_id = os.getenv('SPOTIPY_CLIENT_ID')
    client_secret = os.getenv('SPOTIPY_CLIENT_SECRET')
    redirect_uri = os.getenv('SPOTIPY_REDIRECT_URI')

    if not client_id or not client_secret or not redirect_uri:
        return None
    else:
        return spotipy.oauth2.SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)


def main():
    args = parse_arguments()

    credentials_manager = init_credentials_manager()
    if credentials_manager is None:
        print colored('The necessary environment variables are not set. Please read README.MD more carefully', 'red')
        return 1

    sp = spotipy.Spotify(client_credentials_manager=credentials_manager)

    if args.debug:
        logger.setLevel(logging.DEBUG)
        stdout_level = logging.DEBUG
    else:
        logger.setLevel(logging.CRITICAL)
        eyed3.log.setLevel("ERROR")
        stdout_level = logging.CRITICAL

    data = load_playlist_file(args.file)
    tracks = data['tracks']
    try_read_tags = data['read_id3']

    print colored('Parsed %s tracks from %s' % (len(tracks), args.file.name), 'green')

    for track in tracks:
        if try_read_tags is True:
            id3_data = read_id3_tags(track['path'])
            if id3_data is not None:
                track.update(id3_data, source='id3')
            else:
                guess_data = guess_missing_track_info(track['path'])
                if guess_data is not None:
                    track.update(guess_data, source='guess')
            
        track['spotify_data'] = find_spotify_track(sp, track)
        print format_track_info(track)

    spotify_tracks = [k['spotify_data']['id'] for k in tracks if k.get('spotify_data')]
    spotify_playlist_name = args.file.name
    spotify_username = args.username

    if len(spotify_tracks) < 1:
        print '\nNo tracks matched on Spotify'
        return 1

    print '\n%s/%s of tracks matched on Spotify, creating playlist "%s" on Spotify...' % (
          len(spotify_tracks), len(tracks), spotify_playlist_name)

    token = util.prompt_for_user_token(spotify_username, 'playlist-modify-private')

    if token:
        try:
            sp = spotipy.Spotify(auth=token)
            sp.trace = False
            playlist = sp.user_playlist_create(spotify_username, spotify_playlist_name, public=False)
            if len(spotify_tracks) > 100:
                def chunker(seq, size):
                    return (seq[pos:pos + size] for pos in xrange(0, len(seq), size))

                for spotify_tracks_chunk in chunker(spotify_tracks, 100):
                    results = sp.user_playlist_add_tracks(spotify_username, playlist['id'], spotify_tracks_chunk)
            else:
                results = sp.user_playlist_add_tracks(spotify_username, playlist['id'], spotify_tracks)
        except Exception as e:
            logger.critical('Spotify error: %s' % str(e))
        else:
            print 'done\n'
    else:
        logger.critical('Can\'t get token for %s user' % spotify_username)


if __name__ == "__main__":
    main()
