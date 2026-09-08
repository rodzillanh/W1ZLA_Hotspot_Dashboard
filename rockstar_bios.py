"""Codename Easter egg (v4.5) -- one-line bios + a Wikipedia link for the
deceased rock/roll musician each version's codename honors (see
config.VERSION_CODENAMES). Shown at the bottom of /version.

Like license_quiz.py, this is NOT a network client -- no cache/TTL/fetch,
just a static dict served from memory, keyed by the same bare surname (or
mononym) used in VERSION_CODENAMES.

Written from general knowledge, not independently re-verified against a
live source per person the way this project's other factual claims about
real people usually are (see CLAUDE.md's own discipline around this) --
purely decorative content, not something the app's behavior depends on.
If a date/detail here is ever found to be wrong, fix it the same way a
wrong VERSION_CODENAMES year would be fixed -- don't assume this file is
authoritative over a real source.
"""

BIOS = {
    "Elvis": {
        "name": "Elvis Presley",
        "blurb": "The “King of Rock and Roll,” whose fusion of country, blues, and gospel reshaped American pop music in the 1950s.",
        "wiki": "Elvis_Presley",
    },
    "Bowie": {
        "name": "David Bowie",
        "blurb": "A shapeshifting glam-rock icon known for personas like Ziggy Stardust and albums spanning art rock to soul to electronic.",
        "wiki": "David_Bowie",
    },
    "Lennon": {
        "name": "John Lennon",
        "blurb": "Co-founder of The Beatles and a solo artist and peace activist, shot outside his New York apartment in 1980.",
        "wiki": "John_Lennon",
    },
    "Cobain": {
        "name": "Kurt Cobain",
        "blurb": "Nirvana's frontman, whose album Nevermind brought grunge into the mainstream in 1991.",
        "wiki": "Kurt_Cobain",
    },
    "Petty": {
        "name": "Tom Petty",
        "blurb": "Leader of Tom Petty and the Heartbreakers, known for heartland-rock staples like “Free Fallin'” and “American Girl.”",
        "wiki": "Tom_Petty",
    },
    "Hendrix": {
        "name": "Jimi Hendrix",
        "blurb": "Widely regarded as the greatest electric guitarist in rock history, reshaping the instrument's possibilities in just a few short years.",
        "wiki": "Jimi_Hendrix",
    },
    "Moon": {
        "name": "Keith Moon",
        "blurb": "The Who's explosive, unorthodox drummer, famous for both his playing and his notorious offstage antics.",
        "wiki": "Keith_Moon",
    },
    "Harrison": {
        "name": "George Harrison",
        "blurb": "The Beatles' “quiet” guitarist, who brought Indian instrumentation into rock and later found solo success with “My Sweet Lord.”",
        "wiki": "George_Harrison",
    },
    "Mercury": {
        "name": "Freddie Mercury",
        "blurb": "Queen's flamboyant frontman and one of rock's greatest vocalists, celebrated for his four-octave range and stage presence.",
        "wiki": "Freddie_Mercury",
    },
    "Cornell": {
        "name": "Chris Cornell",
        "blurb": "Frontman of Soundgarden and Audioslave, a defining voice of the Seattle grunge scene.",
        "wiki": "Chris_Cornell",
    },
    "Reed": {
        "name": "Lou Reed",
        "blurb": "Leader of The Velvet Underground and later a solo artist, a foundational figure in art rock and punk.",
        "wiki": "Lou_Reed",
    },
    "Winehouse": {
        "name": "Amy Winehouse",
        "blurb": "A British singer-songwriter whose soul-infused album Back to Black made her one of the defining voices of the 2000s.",
        "wiki": "Amy_Winehouse",
    },
    "Bonham": {
        "name": "John Bonham",
        "blurb": "Led Zeppelin's powerhouse drummer, often cited as one of the most influential drummers in rock history.",
        "wiki": "John_Bonham",
    },
    "Joplin": {
        "name": "Janis Joplin",
        "blurb": "A blues-rock singer known for her raw, powerful vocals, first with Big Brother and the Holding Company.",
        "wiki": "Janis_Joplin",
    },
    "Cocker": {
        "name": "Joe Cocker",
        "blurb": "An English singer known for his gravelly voice and dramatic cover versions, including “With a Little Help from My Friends.”",
        "wiki": "Joe_Cocker",
    },
    "Berry": {
        "name": "Chuck Berry",
        "blurb": "A pioneer of rock and roll whose guitar riffs and showmanship (including the “duck walk”) shaped the genre's foundations.",
        "wiki": "Chuck_Berry",
    },
    "Bennington": {
        "name": "Chester Bennington",
        "blurb": "Linkin Park's lead vocalist, known for blending rap-rock and nu metal with raw emotional intensity.",
        "wiki": "Chester_Bennington",
    },
    "Staley": {
        "name": "Layne Staley",
        "blurb": "Alice in Chains' lead singer, whose dark harmonies helped define the grunge era.",
        "wiki": "Layne_Staley",
    },
    "Ramone": {
        "name": "Joey Ramone",
        "blurb": "Lead singer of the Ramones, a founding figure of the punk rock movement.",
        "wiki": "Joey_Ramone",
    },
    "Vicious": {
        "name": "Sid Vicious",
        "blurb": "Bassist for the Sex Pistols and a notorious punk icon, though better known for image than musicianship.",
        "wiki": "Sid_Vicious",
    },
    "Holly": {
        "name": "Buddy Holly",
        "blurb": "An early rock and roll pioneer whose career was cut short in a 1959 plane crash — “the day the music died.”",
        "wiki": "Buddy_Holly",
    },
    "Orbison": {
        "name": "Roy Orbison",
        "blurb": "Known for his operatic vocal range and songs like “Oh, Pretty Woman” and “Crying.”",
        "wiki": "Roy_Orbison",
    },
    "Cash": {
        "name": "Johnny Cash",
        "blurb": "The “Man in Black,” whose deep baritone and songs like “Ring of Fire” bridged country and rock.",
        "wiki": "Johnny_Cash",
    },
    "Prince": {
        "name": "Prince",
        "blurb": "A prolific singer, songwriter, and multi-instrumentalist whose genre-blending funk-rock defined much of 1980s pop.",
        "wiki": "Prince_(musician)",
    },
    "Morrison": {
        "name": "Jim Morrison",
        "blurb": "The Doors' enigmatic lead singer and poet, part of the “27 Club” of musicians who died at that age.",
        "wiki": "Jim_Morrison",
    },
    "Vaughan": {
        "name": "Stevie Ray Vaughan",
        "blurb": "A blues-rock guitar virtuoso credited with reviving interest in blues guitar in the 1980s.",
        "wiki": "Stevie_Ray_Vaughan",
    },
    "Scott": {
        "name": "Bon Scott",
        "blurb": "AC/DC's original lead singer, whose raspy vocals powered albums like Highway to Hell.",
        "wiki": "Bon_Scott",
    },
    "Bolan": {
        "name": "Marc Bolan",
        "blurb": "Frontman of T. Rex and a key figure in the glam rock movement of the early 1970s.",
        "wiki": "Marc_Bolan",
    },
    "Danko": {
        "name": "Rick Danko",
        "blurb": "Bassist and vocalist for The Band, known for his work on classics like “The Weight.”",
        "wiki": "Rick_Danko",
    },
    "Garcia": {
        "name": "Jerry Garcia",
        "blurb": "Grateful Dead's lead guitarist and de facto leader, central to the jam-band and psychedelic-rock scenes.",
        "wiki": "Jerry_Garcia",
    },
    "Zappa": {
        "name": "Frank Zappa",
        "blurb": "A boundary-pushing composer and guitarist known for genre-defying, satirical work across dozens of albums.",
        "wiki": "Frank_Zappa",
    },
    "Diddley": {
        "name": "Bo Diddley",
        "blurb": "A rock and roll pioneer whose signature “Bo Diddley beat” influenced generations of musicians.",
        "wiki": "Bo_Diddley",
    },
    "Kilmister": {
        "name": "Lemmy Kilmister",
        "blurb": "Motorhead's gravel-voiced bassist and frontman, a defining figure of heavy metal.",
        "wiki": "Lemmy",
    },
    "Hutchence": {
        "name": "Michael Hutchence",
        "blurb": "INXS's charismatic lead singer, who helped bring the band worldwide success in the 1980s.",
        "wiki": "Michael_Hutchence",
    },
    "Buckley": {
        "name": "Jeff Buckley",
        "blurb": "Known for his ethereal voice and the album Grace, particularly his rendition of “Hallelujah.”",
        "wiki": "Jeff_Buckley",
    },
    "Marley": {
        "name": "Bob Marley",
        "blurb": "The most internationally recognized reggae artist, who brought the genre and Rastafarian culture to a global audience.",
        "wiki": "Bob_Marley",
    },
    "Ronson": {
        "name": "Mick Ronson",
        "blurb": "Guitarist and arranger for David Bowie's Spiders from Mars, central to the Ziggy Stardust sound.",
        "wiki": "Mick_Ronson",
    },
    "Curtis": {
        "name": "Ian Curtis",
        "blurb": "Joy Division's haunting frontman, whose post-punk sound and lyrics left a lasting influence on the genre.",
        "wiki": "Ian_Curtis",
    },
    "Bolin": {
        "name": "Tommy Bolin",
        "blurb": "A guitarist who played with Deep Purple and the James Gang, known for his versatile, jazz-influenced style.",
        "wiki": "Tommy_Bolin",
    },
    "Entwistle": {
        "name": "John Entwistle",
        "blurb": "The Who's bassist, nicknamed “The Ox,” known for his intricate, lead-like bass playing.",
        "wiki": "John_Entwistle",
    },
    "Rossington": {
        "name": "Gary Rossington",
        "blurb": "Lynyrd Skynyrd's longtime guitarist and last surviving original member, co-writer of “Free Bird.”",
        "wiki": "Gary_Rossington",
    },
    "Wilson": {
        "name": "Dennis Wilson",
        "blurb": "The Beach Boys' drummer and the only member who actually surfed, later known for his solo album Pacific Ocean Blue.",
        "wiki": "Dennis_Wilson",
    },
    "Burton": {
        "name": "Cliff Burton",
        "blurb": "Metallica's original bassist, whose classically-influenced playing shaped the band's early sound.",
        "wiki": "Cliff_Burton",
    },
    "Watts": {
        "name": "Charlie Watts",
        "blurb": "The Rolling Stones' understated, jazz-schooled drummer for nearly six decades.",
        "wiki": "Charlie_Watts",
    },
    "Moore": {
        "name": "Gary Moore",
        "blurb": "An Irish guitarist known for his work with Thin Lizzy and a celebrated solo blues-rock career.",
        "wiki": "Gary_Moore",
    },
    "Gaines": {
        "name": "Steve Gaines",
        "blurb": "A Lynyrd Skynyrd guitarist who died in the same 1977 plane crash that killed several bandmates, shortly after joining the group.",
        "wiki": "Steve_Gaines",
    },
    "Kath": {
        "name": "Terry Kath",
        "blurb": "Chicago's guitarist and a founding member, praised by Jimi Hendrix as one of his favorite guitarists.",
        "wiki": "Terry_Kath",
    },
    "Whitten": {
        "name": "Danny Whitten",
        "blurb": "Guitarist for Crazy Horse, whose death inspired Neil Young's “The Needle and the Damage Done.”",
        "wiki": "Danny_Whitten",
    },
    "Bloomfield": {
        "name": "Mike Bloomfield",
        "blurb": "A pioneering blues-rock guitarist with the Paul Butterfield Blues Band, and a session player on Dylan's “Like a Rolling Stone.”",
        "wiki": "Mike_Bloomfield",
    },
    "Jones": {
        "name": "Davy Jones",
        "blurb": "Lead singer of The Monkees and a teen-idol face of the 1960s pop/rock TV-band phenomenon.",
        "wiki": "Davy_Jones_(musician)",
    },
    "Frehley": {
        "name": "Ace Frehley",
        "blurb": "KISS's original lead guitarist, the makeup-clad “Spaceman,” known for his melodic solos.",
        "wiki": "Ace_Frehley",
    },
    "Allman": {
        "name": "Duane Allman",
        "blurb": "Co-founder and slide-guitar virtuoso of the Allman Brothers Band, also a sought-after session guitarist.",
        "wiki": "Duane_Allman",
    },
    "Lynott": {
        "name": "Phil Lynott",
        "blurb": "Thin Lizzy's frontman and bassist, known for storytelling lyrics and hits like “The Boys Are Back in Town.”",
        "wiki": "Phil_Lynott",
    },
    "Bruce": {
        "name": "Jack Bruce",
        "blurb": "Cream's bassist and lead vocalist, one of the most influential bass players in rock.",
        "wiki": "Jack_Bruce",
    },
    "Clemons": {
        "name": "Clarence Clemons",
        "blurb": "Saxophonist of Bruce Springsteen's E Street Band, nicknamed “The Big Man.”",
        "wiki": "Clarence_Clemons",
    },
    "Kirwan": {
        "name": "Danny Kirwan",
        "blurb": "An early Fleetwood Mac guitarist and songwriter during the band's blues-rock era, before their pop reinvention.",
        "wiki": "Danny_Kirwan",
    },
    "Winter": {
        "name": "Johnny Winter",
        "blurb": "An albino Texas blues-rock guitarist known for his blistering slide playing and a long solo career alongside session work for Muddy Waters.",
        "wiki": "Johnny_Winter",
    },
    "Balin": {
        "name": "Marty Balin",
        "blurb": "Co-founder and lead vocalist of Jefferson Airplane, a defining voice of the 1960s San Francisco psychedelic rock scene.",
        "wiki": "Marty_Balin",
    },
    "Hopkins": {
        "name": "Nicky Hopkins",
        "blurb": "A prolific session pianist who played on records by the Rolling Stones, the Kinks, the Beatles, and many others without ever being a household name himself.",
        "wiki": "Nicky_Hopkins",
    },
    "Stewart": {
        "name": "Ian Stewart",
        "blurb": "A founding member and pianist of the Rolling Stones, dropped from the official lineup for image reasons but kept on as a touring/session player for the rest of his life.",
        "wiki": "Ian_Stewart_(musician)",
    },
    "Nilsson": {
        "name": "Harry Nilsson",
        "blurb": "A singer-songwriter best known for “Everybody's Talkin'” and his cover of “Without You,” and for a close, hard-partying friendship with John Lennon in the 1970s.",
        "wiki": "Harry_Nilsson",
    },
    "Lee": {
        "name": "Alvin Lee",
        "blurb": "Ten Years After's blazingly fast lead guitarist, best remembered for a marathon performance of “I'm Going Home” at Woodstock in 1969.",
        "wiki": "Alvin_Lee",
    },
    "Grech": {
        "name": "Ric Grech",
        "blurb": "A bassist and violinist who played with Family, the short-lived supergroup Blind Faith, and later Traffic.",
        "wiki": "Ric_Grech",
    },
    "Federici": {
        "name": "Danny Federici",
        "blurb": "A founding member of Bruce Springsteen's E Street Band, known for his organ and glockenspiel work over a four-decade partnership.",
        "wiki": "Danny_Federici",
    },
    "Squire": {
        "name": "Chris Squire",
        "blurb": "Progressive rock band Yes's founding bassist and only member to appear on every one of their studio albums, known for an unusually melodic, treble-heavy bass tone.",
        "wiki": "Chris_Squire",
    },
    "Baker": {
        "name": "Ginger Baker",
        "blurb": "Cream's volatile, jazz-schooled drummer, whose polyrhythmic style and dueling solos with Jack Bruce helped define the power-trio format.",
        "wiki": "Ginger_Baker",
    },
    "Helm": {
        "name": "Levon Helm",
        "blurb": "The Band's drummer and one of its lead vocalists, the only American member of a group otherwise built around Bob Dylan's former Canadian backing musicians.",
        "wiki": "Levon_Helm",
    },
    "Gallagher": {
        "name": "Rory Gallagher",
        "blurb": "An Irish blues-rock guitarist known for relentless touring and a battered, road-worn Fender Stratocaster that became as much his trademark as his playing.",
        "wiki": "Rory_Gallagher",
    },
    "Powell": {
        "name": "Cozy Powell",
        "blurb": "A powerhouse drummer who passed through Rainbow, Whitesnake, and Black Sabbath, known for thunderous fills and a famously huge drum kit.",
        "wiki": "Cozy_Powell",
    },
    "Gatton": {
        "name": "Danny Gatton",
        "blurb": "A virtuoso guitarist nicknamed \"The Humbler\" for his genre-spanning technique, revered by other players despite never breaking through to mainstream fame.",
        "wiki": "Danny_Gatton",
    },
    "Domino": {
        "name": "Fats Domino",
        "blurb": "A New Orleans pianist and singer whose rolling boogie-woogie style helped invent rock and roll, with hits like \"Blueberry Hill\" and \"Ain't That a Shame.\"",
        "wiki": "Fats_Domino",
    },
    "Fogerty": {
        "name": "Tom Fogerty",
        "blurb": "Rhythm guitarist and co-founder of Creedence Clearwater Revival, whose tight chord work anchored the band's run of swamp-rock hits before he left in 1971.",
        "wiki": "Tom_Fogerty",
    },
    "Kramer": {
        "name": "Wayne Kramer",
        "blurb": "Guitarist of Detroit's MC5, whose ferocious, politically charged proto-punk on \"Kick Out the Jams\" made him a lasting influence on punk and hard rock.",
        "wiki": "Wayne_Kramer_(guitarist)",
    },
    "Redding": {
        "name": "Otis Redding",
        "blurb": "Stax Records' defining voice and one of soul music's greatest singers, whose \"(Sittin' On) The Dock of the Bay\" topped the charts weeks after he died in a 1967 plane crash at 26.",
        "wiki": "Otis_Redding",
    },
    "Strummer": {
        "name": "Joe Strummer",
        "blurb": "Frontman, rhythm guitarist and co-songwriter of The Clash, the punk band that pulled reggae, rockabilly and dub into a politically charged sound on \"London Calling\".",
        "wiki": "Joe_Strummer",
    },
    "Hooker": {
        "name": "John Lee Hooker",
        "blurb": "Mississippi-born bluesman whose hypnotic, one-chord boogie and foot-stomping rhythm on records like \"Boogie Chillen'\" became foundational DNA for rock and roll.",
        "wiki": "John_Lee_Hooker",
    },
    "Waters": {
        "name": "Muddy Waters",
        "blurb": "The father of modern Chicago blues, who took the Delta slide sound electric on \"Hoochie Coochie Man\" and \"Rollin' Stone\" — the latter giving the Rolling Stones their name.",
        "wiki": "Muddy_Waters",
    },
    "Perkins": {
        "name": "Carl Perkins",
        "blurb": "Sun Records rockabilly pioneer who wrote and first recorded \"Blue Suede Shoes\" in 1955 — a direct bridge from country and blues into rock and roll, and a touchstone for the Beatles.",
        "wiki": "Carl_Perkins",
    },
    "Vincent": {
        "name": "Gene Vincent",
        "blurb": "Rockabilly wild man whose 1956 hit \"Be-Bop-A-Lula\" — all echo, hiccupping vocals and Cliff Gallup's guitar — became one of early rock and roll's defining records.",
        "wiki": "Gene_Vincent",
    },
    "Valens": {
        "name": "Ritchie Valens",
        "blurb": "Teenage founder of Chicano rock — \"La Bamba\", \"Donna\", \"Come On, Let's Go\" — killed at 17 in the February 1959 plane crash alongside Buddy Holly and the Big Bopper.",
        "wiki": "Ritchie_Valens",
    },
}


def get_bio(codename):
    """Bio dict for a codename, or None if this codename predates the
    Easter egg / isn't in BIOS for some other reason -- callers should
    treat a missing bio as "don't render the card," not an error."""
    return BIOS.get(codename)
