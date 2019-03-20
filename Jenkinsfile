// https://github.com/Rudd-O/shared-jenkins-libraries
@Library('shared-jenkins-libraries@master') _

def RELEASE = funcs.loadParameter('parameters.groovy', 'RELEASE', '28')

def runProgram(thisStage, nextStage, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease) {
	if (mySeparateBoot == "yes") {
		mySeparateBoot = "--separate-boot=boot-${pname}.img"
	} else {
		mySeparateBoot = ""
	}
	if (myBuildFrom == "RPMs") {
		myBuildFrom = "--use-prebuilt-rpms=dist/RELEASE=${myRelease}/"
	} else {
		myBuildFrom = ""
	}
	if (myLuks == "yes") {
		myLuks = "--luks-password=seed"
	} else {
		myLuks = ""
	}
	myRelease = "--releasever=${myRelease}"
	if (mySourceBranch != "") {
		mySourceBranch = "--use-branch=${env.SOURCE_BRANCH}"
	}
	def myShortCircuit = "--short-circuit=${thisStage}"
	def myBreakBefore = ""
	if (nextStage != null) {
		myBreakBefore = "--break-before=${nextStage}"
	}
	def program = """
		yumcache="/mnt/jenkins/yumcache"
		mntdir="\$PWD/mnt/${pname}"
		mkdir -p "\$mntdir"
		volsize=10000
		cmd=src/zfs-fedora-installer/install-fedora-on-zfs
		set -x
		set +e
		ret=0
		ls -l
		sudo \\
			"\$cmd" \\
			${myBuildFrom} \\
			${myShortCircuit} \\
			${myBreakBefore} \\
			${mySourceBranch} \\
			${myLuks} \\
			${mySeparateBoot} \\
			${myRelease} \\
			--trace-file=/dev/stderr \\
			--workdir="\$mntdir" \\
			--yum-cachedir="\$yumcache" \\
			--host-name="\$HOST_NAME" \\
			--pool-name="${pname}" \\
			--vol-size=\$volsize \\
			--swap-size=256 \\
			--root-password=seed \\
			--chown="\$USER" \\
			--chgrp=`groups | cut -d " " -f 1` \\
			--luks-options='-c aes-xts-plain64:sha256 -h sha256 -s 512 --use-random --align-payload 4096' \\
			root-${pname}.img >&2
		ret="\$?"
		#>&2 echo ==============Diagnostics==================
		#>&2 sudo zpool list || true
		#>&2 sudo blkid || true
		#>&2 sudo lsblk || true
		#>&2 sudo losetup -la || true
		#>&2 sudo mount || true
		#>&2 echo Return value of program: "\$ret"
		#>&2 echo =========== End Diagnostics ===============
		if [ "\$ret" == "120" ] ; then ret=0 ; fi
		exit "\$ret"
	""".stripIndent().trim()
	return program
}

def runStage(thisStage, allStages, paramShortCircuit, paramBreakBefore, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, theIt) {
	def thisStageIdx = allStages.findIndexOf{ s -> s == thisStage }
	def nextStage = allStages[thisStageIdx + 1]
	def paramShortCircuitIdx = allStages.findIndexOf{ s -> s == paramShortCircuit }
	def paramBreakBeforeIdx = allStages.findIndexOf{ s -> s == paramBreakBefore }
	def whenCond = ((paramShortCircuit == "" || paramShortCircuitIdx <= thisStageIdx) && (paramBreakBefore == "" || paramBreakBeforeIdx > thisStageIdx))
	def stageName = thisStage.toString().capitalize().replace('_', ' ')
	stage("${stageName} ${theIt.join(' ')}") {
		when (whenCond) {
			def program = runProgram(thisStage, nextStage, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease)
			def desc = "============= REPORT ==============\nPool name: ${pname}\nBranch name: ${env.BRANCH_NAME}\nGit hash: ${env.GIT_HASH}\nRelease: ${myRelease}\nBuild from: ${myBuildFrom}\nLUKS: ${myLuks}\nSeparate boot: ${mySeparateBoot}\nSource branch: ${env.SOURCE_BRANCH}\n============= END REPORT =============="
			println "${desc}\n\n" + "Program that will be executed:\n${program}"
			sh program
		}
	}
}

pipeline {

	agent none

	options {
		checkoutToSubdirectory 'src/zfs-fedora-installer'
		disableConcurrentBuilds()
	}

	triggers {
		upstream(
			upstreamProjects: 'ZFS/master,ZFS/staging',
			threshold: hudson.model.Result.SUCCESS
		)
	}

	parameters {
		string defaultValue: 'ZFS/master', description: '', name: 'UPSTREAM_PROJECT', trim: true
		string defaultValue: 'master', description: '', name: 'SOURCE_BRANCH', trim: true
		string defaultValue: 'grub-zfs-fixer (master)', description: '', name: 'GRUB_UPSTREAM_PROJECT', trim: true
		string defaultValue: 'yes', description: '', name: 'BUILD_FROM_SOURCE', trim: true
		string defaultValue: 'yes', description: '', name: 'BUILD_FROM_RPMS', trim: true
		string defaultValue: 'seed', description: '', name: 'POOL_NAME', trim: true
		string defaultValue: 'seed.dragonfear', description: '', name: 'HOST_NAME', trim: true
		string defaultValue: 'yes no', description: '', name: 'SEPARATE_BOOT', trim: true
		string defaultValue: 'yes no', description: '', name: 'LUKS', trim: true
		string defaultValue: '', description: 'Stop before this stage.', name: 'BREAK_BEFORE', trim: true
		string defaultValue: '', description: 'Start with this stage.  If this variable is defined, the disk images from prior builds will not be cleaned up prior to short-circuiting to this stage.', name: 'SHORT_CIRCUIT', trim: true
		string defaultValue: '', description: "Override which Fedora releases to build for.  If empty, defaults to ${RELEASE}.", name: 'RELEASE', trim: true
	}

	stages {
		stage('Preparation') {
			agent { label 'master' }
			steps {
				script {
					funcs.announceBeginning()
				}
				script {
					env.GIT_HASH = sh (
						script: "cd src/zfs-fedora-installer && git rev-parse --short HEAD",
						returnStdout: true
					).trim()
					println "Git hash is reported as ${env.GIT_HASH}"
				}
			}
		}
		stage('Setup environment') {
			agent { label 'master' }
			steps {
				script {
					env.GRUB_UPSTREAM_PROJECT = params.GRUB_UPSTREAM_PROJECT
					if (funcs.isUpstreamCause(currentBuild)) {
						def upstreamProject = funcs.getUpstreamProject(currentBuild)
						if (env.BRANCH_NAME != "master") {
							currentBuild.description = "Skipped test triggered by upstream job ${upstreamProject} because this test is from the ${env.BRANCH_NAME} branch of zfs-fedora-installer."
							currentBuild.result = 'NOT_BUILT'
							return
						}
						env.UPSTREAM_PROJECT = upstreamProject
						env.SOURCE_BRANCH = ""
						env.BUILD_FROM_SOURCE = "no"
						env.BUILD_FROM_RPMS = "yes"
					} else {
						env.UPSTREAM_PROJECT = params.UPSTREAM_PROJECT
						env.SOURCE_BRANCH = params.SOURCE_BRANCH
						env.BUILD_FROM_SOURCE = params.BUILD_FROM_SOURCE
						env.BUILD_FROM_RPMS = params.BUILD_FROM_RPMS
					}
					if (env.UPSTREAM_PROJECT == "") {
						currentBuild.result = 'ABORTED'
						error("UPSTREAM_PROJECT must be set to a project containing built ZFS RPMs.")
					}
					if (env.BUILD_FROM_SOURCE == "yes" && env.BUILD_FROM_RPMS == "yes") {
						env.BUILD_FROM = "source RPMs"
					} else if (env.BUILD_FROM_SOURCE == "yes" && env.BUILD_FROM_RPMS == "no") {
						env.BUILD_FROM = "source"
					} else if (env.BUILD_FROM_SOURCE == "no" && env.BUILD_FROM_RPMS == "yes") {
						env.BUILD_FROM = "RPMs"
					} else {
						currentBuild.result = 'ABORTED'
						error("At least one of BUILD_FROM_SOURCE and BUILD_FROM_RPMS must be set to yes.")
					}
					if (env.BUILD_FROM_SOURCE == "yes" && env.SOURCE_BRANCH == "") {
						currentBuild.result = 'ABORTED'
						error("SOURCE_BRANCH must be set when BUILD_FROM_SOURCE is set to yes.")
					}
					env.BUILD_TRIGGER = funcs.describeCause(currentBuild)
					currentBuild.description = "Test of ${env.BUILD_FROM} from source branch ${env.SOURCE_BRANCH} and RPMs from ${env.UPSTREAM_PROJECT}.  ${env.BUILD_TRIGGER}."
				}
			}
		}
		stage('Copy from master') {
			agent { label 'master' }
			when { allOf { not { equals expected: 'NOT_BUILT', actual: currentBuild.result }; equals expected: "", actual: params.SHORT_CIRCUIT } }
			steps {
				sh "rm -rf build dist"
				copyArtifacts(
					projectName: env.UPSTREAM_PROJECT,
					fingerprintArtifacts: true,
					selector: upstream(fallbackToLastSuccessful: true)
				)
				copyArtifacts(
					projectName: env.GRUB_UPSTREAM_PROJECT,
					fingerprintArtifacts: true,
					selector: upstream(fallbackToLastSuccessful: true)
				)
				sh 'for d in dist/RELEASE=* ; do cp -a dist/grub-zfs-fixer*rpm $d ; done'
				sh 'find dist/RELEASE=* -type f | tee /dev/stderr | sort | grep -v debuginfo | grep -v debugsource | xargs sha256sum > rpmsums'
				sh 'cp -a "$JENKINS_HOME"/userContent/activate-zfs-in-qubes-vm .'
				stash includes: 'dist/RELEASE=*/**', name: 'rpms', excludes: '**/*debuginfo*,**/*debugsource*'
				stash includes: 'rpmsums', name: 'rpmsums'
				stash includes: 'activate-zfs-in-qubes-vm', name: 'activate-zfs-in-qubes-vm'
				stash includes: 'src/zfs-fedora-installer/**', name: 'zfs-fedora-installer'
			}
		}
		stage('Parallelize') {
			agent { label 'master' }
			when { not { equals expected: 'NOT_BUILT', actual: currentBuild.result } }
			steps {
				script {
					if (params.RELEASE != '') {
						RELEASE = params.RELEASE
					}
					def axisList = [
						RELEASE.split(' '),
						env.BUILD_FROM.split(' '),
						params.LUKS.split(' '),
						params.SEPARATE_BOOT.split(' '),
					]
					def task = {
						def myRelease = it[0]
						def myBuildFrom = it[1]
						def myLuks = it[2]
						def mySeparateBoot = it[3]
						def pname = "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.GIT_HASH}_${myRelease}_${myBuildFrom}_${myLuks}_${mySeparateBoot}"
						def mySourceBranch = ""
						if (env.SOURCE_BRANCH != "") {
							mySourceBranch = env.SOURCE_BRANCH
						}
						return {
							node('fedorazfs') {
								lock("activatezfs") {
								stage("Install deps ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
									timeout(time: 10, unit: 'MINUTES') {
										def program = '''
												deps="rsync e2fsprogs dosfstools cryptsetup qemu gdisk python2"
												rpm -q \$deps || sudo dnf install -qy \$deps
										'''.stripIndent().trim()
										println "Program that will be executed:\n${program}"
										retry(2) {
											sh program
										}
									}
                                                                        }
								}
                                                                }
								lock("activatezfs") {
								stage("Activate ZFS ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
									timeout(time: 10, unit: 'MINUTES') {
										unstash "activate-zfs-in-qubes-vm"
										sh 'find dist/RELEASE=* -type f | tee /dev/stderr | sort | grep -v debuginfo | grep -v debugsource | xargs sha256sum > local-rpmsums'
										unstash "rpmsums"
										def needsunstash = sh (
											script: '''
											set +e ; set -x
											output=$(diff -Naur local-rpmsums rpmsums 2>&1)
											if [ "$?" = "0" ]
											then
												echo MATCH
											else
												echo "$output" >&2
											fi
											''',
											returnStdout: true
										).trim()
										if (needsunstash != "MATCH") {
											unstash "rpms"
										}
										def program = """
											release=`rpm -q --queryformat="%{version}" fedora-release`
											sudo ./activate-zfs-in-qubes-vm dist/RELEASE=\$release/
										""".stripIndent().trim()
										println "Program that will be executed:\n${program}"
										retry(2) {
											sh program
										}
									}
                                                                        }
								}
                                                                }
								stage("Unstash ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
										unstash "zfs-fedora-installer"
									}
								}
								stage("Remove old image ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
										sh "rm -rf root-${pname}.img boot-${pname}.img ${pname}.log"
									}
								}
								timeout(30) {
								runStage("beginning",
									 ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
									 params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(5) {
								runStage("reload_chroot",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
									 params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(15) {
								runStage("bootloader_install",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
                                                                         params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(10) {
								runStage("boot_to_test_non_hostonly",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
                                                                         params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(10) {
								runStage("boot_to_test_hostonly",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
                                                                         params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
							}
						}
					}
					parallel funcs.combo(task, axisList)
				}
			}
		}
	}
	post {
		always {
			node('master') {
				script {
					funcs.announceEnd(currentBuild.currentResult)
				}
			}
		}
	}
}
